#!/usr/bin/env python3
# Copyright 2024 OpenStack Operator
# SPDX-License-Identifier: Apache-2.0
"""
Juju charm for OpenStack Skyline Dashboard (stable/2024.2).

Install sequence
----------------
1.  apt: baseline packages + console build deps + (optional) mariadb
2.  python3 -m venv /opt/skyline-venv
3.  git clone skyline-apiserver → pip install into venv
4.  git clone skyline-console → nvm/Node/yarn → make package → pip install wheel
5.  MariaDB: create database + user  (local mode only)
6.  Render /etc/skyline/skyline.yaml, gunicorn.py, systemd unit, nginx.conf
7.  systemctl daemon-reload
8.  make db_sync  (idempotent Alembic migration)
9.  Enable + (re)start all services

config-changed
--------------
Re-renders all templates, re-runs db_sync, reloads/restarts services.
"""

import json
import logging
import os
import secrets
import subprocess
import textwrap
from pathlib import Path

import ops
from jinja2 import Environment, FileSystemLoader

logger = logging.getLogger(__name__)

# ── Filesystem paths ────────────────────────────────────────────────────────
VENV_DIR = Path("/opt/skyline-venv")
VENV_BIN = VENV_DIR / "bin"
VENV_PY = VENV_BIN / "python3"
VENV_PIP = VENV_BIN / "pip"

APISERVER_SRC = Path("/opt/skyline-apiserver-src")
CONSOLE_SRC = Path("/opt/skyline-console-src")

SKYLINE_CONF_DIR = Path("/etc/skyline")
SKYLINE_LOG_DIR = Path("/var/log/skyline")
SKYLINE_POLICY_DIR = SKYLINE_CONF_DIR / "policy"

SYSTEMD_UNIT_PATH = Path("/etc/systemd/system/skyline-apiserver.service")
NGINX_CONF_PATH = Path("/etc/nginx/nginx.conf")
GUNICORN_CONF_PATH = SKYLINE_CONF_DIR / "gunicorn.py"
SKYLINE_YAML_PATH = SKYLINE_CONF_DIR / "skyline.yaml"

NVM_DIR = Path("/root/.nvm")


# ── Charm class ─────────────────────────────────────────────────────────────
class SkylineCharm(ops.CharmBase):
    """Juju charm deploying the OpenStack Skyline Dashboard."""

    _stored = ops.StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self._stored.set_default(
            installed=False,
            secret_key="",       # generated once, persisted across config changes
            db_password="",      # generated once for local MariaDB
            static_path="",      # discovered after console wheel is installed
        )
        # Core lifecycle
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)

        # Actions
        self.framework.observe(self.on.db_sync_action, self._on_action_db_sync)
        self.framework.observe(self.on.get_static_path_action, self._on_action_get_static_path)
        self.framework.observe(self.on.restart_services_action, self._on_action_restart_services)
        self.framework.observe(self.on.show_config_action, self._on_action_show_config)

    # ── Low-level helpers ───────────────────────────────────────────────────

    def _run(self, cmd, input_data=None, env=None, cwd=None, capture=False):
        """Run a command, raising CalledProcessError on failure."""
        logger.debug("run: %s", " ".join(str(x) for x in cmd))
        kwargs = dict(check=True, cwd=cwd, env=env)
        if input_data is not None:
            kwargs["input"] = input_data
        if capture:
            kwargs["capture_output"] = True
            kwargs["text"] = True
        return subprocess.run(cmd, **kwargs)

    def _apt_install(self, packages: list):
        env = os.environ.copy()
        env["DEBIAN_FRONTEND"] = "noninteractive"
        self._run(["apt-get", "update", "-qq"], env=env)
        self._run(
            ["apt-get", "install", "-y", "--no-install-recommends"] + packages,
            env=env,
        )

    def _pip(self, args: list):
        self._run([str(VENV_PIP)] + args)

    def _nvm_sh(self, script: str):
        """Run a shell script with nvm loaded from /root/.nvm/nvm.sh."""
        loader = (
            f'export HOME=/root; '
            f'export NVM_DIR="{NVM_DIR}"; '
            f'[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"; '
        )
        self._run(["bash", "-c", loader + script])

    def _render_template(self, template_name: str, dest: Path, context: dict):
        tmpl_dir = Path(self.charm_dir) / "templates"
        env = Environment(
            loader=FileSystemLoader(str(tmpl_dir)),
            autoescape=False,
            keep_trailing_newline=True,
        )
        content = env.get_template(template_name).render(**context)
        dest.write_text(content, encoding="utf-8")
        logger.info("Rendered %s → %s", template_name, dest)

    def _venv_env(self) -> dict:
        """Return an env dict with venv/bin prepended to PATH."""
        env = os.environ.copy()
        env["PATH"] = f"{VENV_BIN}:{env.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin')}"
        env["OS_CONFIG_DIR"] = str(SKYLINE_CONF_DIR)
        env["VIRTUAL_ENV"] = str(VENV_DIR)
        return env

    # ── Config helpers ──────────────────────────────────────────────────────

    def _using_local_db(self) -> bool:
        return not bool(self.config.get("database-url", "").strip())

    def _db_password(self) -> str:
        """Return (and persist) the local MariaDB password."""
        if not self._stored.db_password:
            cfg = self.config.get("database-password", "").strip()
            self._stored.db_password = cfg if cfg else secrets.token_urlsafe(20)
        return self._stored.db_password

    def _secret_key(self) -> str:
        """Return (and persist) the session secret key."""
        if not self._stored.secret_key:
            cfg = self.config.get("secret-key", "").strip()
            self._stored.secret_key = cfg if cfg else secrets.token_urlsafe(32)
        return self._stored.secret_key

    def _database_url(self) -> str:
        if not self._using_local_db():
            return self.config["database-url"].strip()
        return f"mysql://skyline:{self._db_password()}@localhost:3306/skyline"

    def _keystone_url(self) -> str:
        url = self.config.get("keystone-url", "").strip().rstrip("/")
        if not url.endswith("/v3"):
            url += "/v3"
        return url + "/"

    def _missing_required_config(self) -> str:
        """Return an error message if required config is missing, else empty string."""
        if not self.config.get("keystone-url", "").strip():
            return "Required config 'keystone-url' is not set"
        if not self.config.get("system-user-password", "").strip():
            return "Required config 'system-user-password' is not set"
        return ""

    def _template_context(self) -> dict:
        cfg = self.config
        workers = cfg["gunicorn-workers"]
        return {
            # database
            "database_url": self._database_url(),
            # openstack
            "keystone_url": self._keystone_url(),
            "default_region": cfg["default-region"],
            "system_user_name": cfg["system-user-name"],
            "system_user_password": cfg["system-user-password"],
            "system_user_domain": cfg["system-user-domain"],
            "system_project": cfg["system-project"],
            "system_project_domain": cfg["system-project-domain"],
            "interface_type": cfg["interface-type"],
            "sso_enabled": cfg["sso-enabled"],
            "sso_region": cfg["sso-region"],
            "enforce_new_defaults": cfg["enforce-new-defaults"],
            "reclaim_instance_interval": cfg["reclaim-instance-interval"],
            # skyline defaults
            "debug": cfg["debug"],
            "ssl_enabled": cfg["ssl-enabled"],
            "secret_key": self._secret_key(),
            "prometheus_endpoint": cfg.get("prometheus-endpoint", "").strip(),
            "prometheus_enable_basic_auth": cfg["prometheus-enable-basic-auth"],
            "prometheus_basic_auth_user": cfg.get("prometheus-basic-auth-user", ""),
            "prometheus_basic_auth_password": cfg.get("prometheus-basic-auth-password", ""),
            # nginx / gunicorn
            "listen_port": cfg["listen-port"],
            "static_path": self._stored.static_path,
            "gunicorn_workers": workers if workers > 0 else "__CPU_COUNT__",
            "gunicorn_timeout": cfg["gunicorn-timeout"],
            # paths
            "venv_dir": str(VENV_DIR),
            "skyline_conf_dir": str(SKYLINE_CONF_DIR),
            "skyline_log_dir": str(SKYLINE_LOG_DIR),
            # flags
            "using_local_db": self._using_local_db(),
        }

    # ── Installation phase ──────────────────────────────────────────────────

    def _install_system_packages(self):
        self.unit.status = ops.MaintenanceStatus("Installing system packages")
        baseline = [
            "ca-certificates", "git", "curl", "wget",
            "python3", "python3-pip", "python3-venv",
            "build-essential", "make",
            "nginx", "ssl-cert",
        ]
        # Build dependencies for skyline-console on Ubuntu 22.04 (Jammy)
        console_build_deps = [
            "libgtk2.0-0", "libgtk-3-0", "libgbm-dev", "libnotify-dev",
            "libnss3", "libxss1", "libasound2", "libxtst6",
            "libgconf-2-4", "xauth", "xvfb",
        ]
        self._apt_install(baseline + console_build_deps)

        if self._using_local_db():
            self.unit.status = ops.MaintenanceStatus("Installing MariaDB")
            self._apt_install(["mariadb-server"])
            self._run(["systemctl", "enable", "mariadb"])
            self._run(["systemctl", "start", "mariadb"])

    def _setup_venv(self):
        self.unit.status = ops.MaintenanceStatus("Creating Python virtualenv")
        self._run(["python3", "-m", "venv", str(VENV_DIR)])
        self._pip(["install", "--upgrade", "pip", "wheel", "setuptools"])

    def _install_apiserver(self, upgrade: bool = False):
        self.unit.status = ops.MaintenanceStatus("Installing skyline-apiserver")
        branch = self.config["apiserver-branch"]

        if APISERVER_SRC.exists():
            if upgrade:
                self._run(["git", "-C", str(APISERVER_SRC), "pull"])
        else:
            self._run([
                "git", "clone",
                "https://opendev.org/openstack/skyline-apiserver.git",
                "--branch", branch, "--single-branch",
                str(APISERVER_SRC),
            ])

        # Install the package. Use --upgrade so that a git pull that didn't
        # bump the version string still forces pip to reinstall from source.
        self._pip(["install", "--upgrade", str(APISERVER_SRC)])

    def _install_nvm(self):
        self.unit.status = ops.MaintenanceStatus("Installing nvm")
        nvm_script = Path("/tmp/nvm-install.sh")
        self._run([
            "wget", "-q", "-O", str(nvm_script),
            "https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh",
        ])
        # HOME must be /root so nvm installs to /root/.nvm — the same path
        # that _nvm_sh() exports. Without this, nvm may install elsewhere if
        # the Juju hook runner has a different HOME.
        env = os.environ.copy()
        env["HOME"] = "/root"
        self._run(["bash", str(nvm_script)], env=env)

    def _detect_node_lts_alias(self) -> str:
        """
        Read engines.node from skyline-console/package.json and map it to an
        nvm LTS alias.  Falls back to 'gallium' (Node 16) which is what the
        upstream 2024.2 documentation specifies.
        """
        pkg_json = CONSOLE_SRC / "package.json"
        try:
            data = json.loads(pkg_json.read_text())
            node_req = data.get("engines", {}).get("node", "")
            logger.info("package.json engines.node = %r", node_req)
            if "20" in node_req:
                return "iron"       # Node 20 LTS
            if "18" in node_req:
                return "hydrogen"   # Node 18 LTS
        except Exception as exc:
            logger.warning("Could not read package.json engines: %s", exc)
        return "gallium"            # Node 16 LTS — upstream default for 2024.2

    def _install_console(self, upgrade: bool = False):
        """Clone, build, and install the skyline-console Python wheel."""
        branch = self.config["console-branch"]

        # ── Clone / update ──────────────────────────────────────────────
        if CONSOLE_SRC.exists():
            if upgrade:
                self.unit.status = ops.MaintenanceStatus("Updating skyline-console source")
                self._run(["git", "-C", str(CONSOLE_SRC), "pull"])
        else:
            self.unit.status = ops.MaintenanceStatus("Cloning skyline-console")
            self._run([
                "git", "clone",
                "https://opendev.org/openstack/skyline-console.git",
                "--branch", branch, "--single-branch",
                str(CONSOLE_SRC),
            ])

        # ── nvm ─────────────────────────────────────────────────────────
        if not (NVM_DIR / "nvm.sh").exists():
            self._install_nvm()

        lts_alias = self._detect_node_lts_alias()

        # ── Node.js + yarn ───────────────────────────────────────────────
        self.unit.status = ops.MaintenanceStatus(f"Installing Node.js (lts/{lts_alias})")
        self._nvm_sh(f"nvm install --lts={lts_alias}")
        self._nvm_sh(f"nvm alias default lts/{lts_alias}")
        self._nvm_sh(f"nvm use lts/{lts_alias} && npm install -g yarn")

        # ── Build the React app into a Python wheel ──────────────────────
        self.unit.status = ops.MaintenanceStatus(
            "Building skyline-console wheel (takes several minutes)"
        )
        self._nvm_sh(f"nvm use lts/{lts_alias} && cd {CONSOLE_SRC} && make package")

        wheels = sorted(CONSOLE_SRC.glob("dist/skyline_console-*.whl"))
        if not wheels:
            raise RuntimeError(
                "make package succeeded but no .whl found in "
                f"{CONSOLE_SRC}/dist/"
            )

        # ── Install wheel into venv ──────────────────────────────────────
        self.unit.status = ops.MaintenanceStatus("Installing skyline-console wheel")
        self._pip(["install", "--force-reinstall", str(wheels[-1])])

        # ── Discover static files path ───────────────────────────────────
        result = self._run(
            [
                str(VENV_PY), "-c",
                "import skyline_console, os; "
                "print(os.path.join(os.path.dirname(skyline_console.__file__), 'static'))",
            ],
            capture=True,
        )
        self._stored.static_path = result.stdout.strip()
        logger.info("Console static path: %s", self._stored.static_path)

    # ── Database setup (local MariaDB only) ─────────────────────────────────

    def _setup_local_mariadb(self):
        self.unit.status = ops.MaintenanceStatus("Configuring local MariaDB")
        db_pass = self._db_password()
        # Use double quotes inside SQL to avoid shell quoting issues
        sql = textwrap.dedent(f"""\
            CREATE DATABASE IF NOT EXISTS skyline
              DEFAULT CHARACTER SET utf8
              DEFAULT COLLATE utf8_general_ci;
            GRANT ALL PRIVILEGES ON skyline.* TO 'skyline'@'localhost'
              IDENTIFIED BY '{db_pass}';
            GRANT ALL PRIVILEGES ON skyline.* TO 'skyline'@'%'
              IDENTIFIED BY '{db_pass}';
            FLUSH PRIVILEGES;
        """)
        self._run(["mysql", "-u", "root"], input_data=sql.encode())
        logger.info("MariaDB skyline database and user created/verified.")

    # ── Configuration rendering ─────────────────────────────────────────────

    def _ensure_directories(self):
        for d in [SKYLINE_CONF_DIR, SKYLINE_LOG_DIR, SKYLINE_POLICY_DIR]:
            d.mkdir(parents=True, exist_ok=True)

    def _run_db_sync(self):
        """Run alembic database migration via make db_sync (idempotent)."""
        self.unit.status = ops.MaintenanceStatus("Running database migration (db_sync)")
        env = self._venv_env()
        self._run(["make", "db_sync"], cwd=str(APISERVER_SRC), env=env)
        logger.info("db_sync completed successfully.")

    def _configure(self):
        """Render all configuration files and restart services."""
        error = self._missing_required_config()
        if error:
            self.unit.status = ops.BlockedStatus(error)
            return False

        self.unit.status = ops.MaintenanceStatus("Rendering configuration")
        self._ensure_directories()

        # Setup local MariaDB database/user on every config-changed so that
        # a password change is picked up.  CREATE DATABASE IF NOT EXISTS and
        # GRANT ... are idempotent.
        if self._using_local_db():
            self._setup_local_mariadb()

        ctx = self._template_context()

        self._render_template("skyline.yaml.j2", SKYLINE_YAML_PATH, ctx)
        self._render_template("gunicorn.py.j2", GUNICORN_CONF_PATH, ctx)
        self._render_template("skyline-apiserver.service.j2", SYSTEMD_UNIT_PATH, ctx)

        if self._stored.static_path:
            self._render_template("nginx.conf.j2", NGINX_CONF_PATH, ctx)
            nginx_ready = True
        else:
            logger.warning(
                "static_path not yet set — nginx.conf not rendered. "
                "Run 'juju run-action skyline/0 restart-services' after install completes."
            )
            nginx_ready = False

        self._run(["systemctl", "daemon-reload"])
        self._run_db_sync()
        self._restart_services(nginx=nginx_ready)
        return True

    # ── Service management ──────────────────────────────────────────────────

    def _restart_services(self, nginx: bool = True):
        """Enable and restart / reload all Skyline-related services."""
        if self._using_local_db():
            self._run(["systemctl", "enable", "--now", "mariadb"])

        self._run(["systemctl", "enable", "skyline-apiserver"])
        self._run(["systemctl", "restart", "skyline-apiserver"])

        if nginx:
            self._run(["nginx", "-t"])
            self._run(["systemctl", "enable", "nginx"])
            self._run(["systemctl", "reload-or-restart", "nginx"])

    # ── Lifecycle event handlers ────────────────────────────────────────────

    def _on_install(self, event: ops.InstallEvent):
        self.unit.status = ops.MaintenanceStatus("Starting Skyline installation")
        try:
            self._install_system_packages()
            self._setup_venv()
            self._install_apiserver()
            self._install_console()
            self._stored.installed = True
            logger.info("Skyline software installation complete.")
            self.unit.status = ops.MaintenanceStatus("Software installed; awaiting config")
        except Exception as exc:
            logger.exception("Installation failed")
            self.unit.status = ops.BlockedStatus(f"Install failed: {exc}")

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        if not self._stored.installed:
            self.unit.status = ops.WaitingStatus("Waiting for install to complete")
            event.defer()
            return
        try:
            ok = self._configure()
            if ok:
                self.unit.status = ops.ActiveStatus(
                    f"Skyline ready on :{self.config['listen-port']}"
                )
        except Exception as exc:
            logger.exception("config-changed failed")
            self.unit.status = ops.BlockedStatus(f"Config error: {exc}")

    def _on_start(self, event: ops.StartEvent):
        if not self._stored.installed:
            self.unit.status = ops.WaitingStatus("Waiting for install to complete")
            event.defer()
            return
        # Services were already started by _configure() in config-changed.
        # Just confirm status here.
        try:
            result = self._run(
                ["systemctl", "is-active", "skyline-apiserver"],
                capture=True,
            )
            if result.stdout.strip() == "active":
                self.unit.status = ops.ActiveStatus(
                    f"Skyline ready on :{self.config['listen-port']}"
                )
            else:
                self.unit.status = ops.BlockedStatus("skyline-apiserver is not active")
        except subprocess.CalledProcessError:
            self.unit.status = ops.BlockedStatus("skyline-apiserver failed to start")

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent):
        self.unit.status = ops.MaintenanceStatus("Upgrading Skyline charm")
        try:
            self._install_apiserver(upgrade=True)
            self._install_console(upgrade=True)
            ok = self._configure()
            if ok:
                self.unit.status = ops.ActiveStatus(
                    f"Skyline ready on :{self.config['listen-port']}"
                )
        except Exception as exc:
            logger.exception("upgrade-charm failed")
            self.unit.status = ops.BlockedStatus(f"Upgrade failed: {exc}")

    # ── Action handlers ─────────────────────────────────────────────────────

    def _on_action_db_sync(self, event: ops.ActionEvent):
        try:
            self._run_db_sync()
            event.set_results({"result": "db_sync completed successfully"})
        except Exception as exc:
            event.fail(f"db_sync failed: {exc}")

    def _on_action_get_static_path(self, event: ops.ActionEvent):
        event.set_results({"static-path": self._stored.static_path or "(not yet discovered)"})

    def _on_action_restart_services(self, event: ops.ActionEvent):
        try:
            nginx_ready = bool(self._stored.static_path)
            self._restart_services(nginx=nginx_ready)
            event.set_results({"result": "Services restarted/reloaded"})
        except Exception as exc:
            event.fail(f"Restart failed: {exc}")

    def _on_action_show_config(self, event: ops.ActionEvent):
        try:
            content = SKYLINE_YAML_PATH.read_text() if SKYLINE_YAML_PATH.exists() else "(not yet rendered)"
            event.set_results({"skyline-yaml": content})
        except Exception as exc:
            event.fail(str(exc))


if __name__ == "__main__":
    ops.main(SkylineCharm)
