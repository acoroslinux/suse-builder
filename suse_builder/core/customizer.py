import subprocess
from pathlib import Path
from typing import Dict, Any
import logging
from suse_builder.core.chroot_manager import ChrootManager

logger = logging.getLogger("customizer")

class SystemCustomizer:
    def __init__(self, chroot: ChrootManager, config: Dict[str, Any]):
        self.chroot = chroot
        self.config = config
        self.target_root = chroot.target_root

    def setup_live_users(self):
        if self.chroot.mode == "mock":
            return
        import shutil
        live_user_cfg = self.config.get("live_user", "liveuser")
        if isinstance(live_user_cfg, dict):
            live_user = live_user_cfg.get("name", "liveuser")
            live_password = live_user_cfg.get("password", "live")
            cfg_groups = live_user_cfg.get("groups", [])
        else:
            live_user = str(live_user_cfg)
            live_password = "live"
            cfg_groups = []

        # All essential desktop and administrative groups
        groups = self.config.get("live_groups") or cfg_groups or [
            "wheel", "sudo", "audio", "video", "render", "input", "seat", "disk", "storage", "users", "autologin"
        ]

        try:
            # 1. Create all missing groups
            for group in groups:
                lookup = self.chroot.run_in_chroot(["getent", "group", str(group)], check=False)
                if lookup.returncode != 0:
                    self.chroot.run_in_chroot(["groupadd", "-f", str(group)], check=False)

            # 2. Check if user already exists
            user_check = self.chroot.run_in_chroot(["id", "-u", str(live_user)], check=False)
            if user_check.returncode != 0:
                res = self.chroot.run_in_chroot(["useradd", "-m", "-s", "/bin/bash", "-U", str(live_user)], check=False)
                if res.returncode != 0:
                    self.chroot.run_in_chroot(["useradd", "-m", "-s", "/bin/bash", str(live_user)], check=False)

            # 3. Add to groups individually so a missing group never aborts everything
            for group in groups:
                self.chroot.run_in_chroot(["usermod", "-aG", str(group), str(live_user)], check=False)

            # 4. Set passwords for liveuser and root
            self.chroot.run_in_chroot(f"echo '{live_user}:{live_password}' | chpasswd", check=False)
            self.chroot.run_in_chroot(f"echo 'root:{live_password}' | chpasswd", check=False)

            # 5. Unlock accounts
            self.chroot.run_in_chroot(["passwd", "-u", str(live_user)], check=False)
            self.chroot.run_in_chroot(["passwd", "-u", "root"], check=False)

            # 6. Ensure home directory exists and is populated from /etc/skel
            home_dir = self.target_root / "home" / live_user
            home_dir.mkdir(parents=True, exist_ok=True)
            skel_dir = self.target_root / "etc" / "skel"
            if skel_dir.is_dir():
                for item in skel_dir.iterdir():
                    dest = home_dir / item.name
                    if not dest.exists():
                        if item.is_dir():
                            shutil.copytree(item, dest, symlinks=True, ignore_dangling_symlinks=True)
                        else:
                            shutil.copy2(item, dest)

            # Create standard XDG directories (Downloads, Documents, etc.)
            self.chroot.run_in_chroot(["su", "-", live_user, "-c", "xdg-user-dirs-update --force"], check=False)

            self.chroot.run_in_chroot(["chown", "-R", f"{live_user}:{live_user}", f"/home/{live_user}"], check=False)
            self.chroot.run_in_chroot(["chmod", "755", f"/home/{live_user}"], check=False)
        except Exception:
            logger.exception("Could not fully configure live user %s", live_user)

        sudoers_file = self.target_root / "etc" / "sudoers.d" / "10-liveuser"
        sudoers_file.parent.mkdir(parents=True, exist_ok=True)
        sudoers_file.write_text(f"{live_user} ALL=(ALL) NOPASSWD: ALL\n%wheel ALL=(ALL) NOPASSWD: ALL\n")
        try:
            sudoers_file.chmod(0o440)
            self.chroot.run_in_chroot(["chown", "-R", "root:root", "/etc/sudoers.d"], check=False)
        except Exception:
            pass

    def configure_system_defaults(self):
        if self.chroot.mode == "mock":
            return
        hostname = self.config.get("hostname", "opensuse-modern")
        etc_dir = self.target_root / "etc"
        etc_dir.mkdir(parents=True, exist_ok=True)
        with open(etc_dir / "hostname", "w") as f:
            f.write(f"{hostname}\n")

        hosts_file = etc_dir / "hosts"
        hosts_content = (
            "127.0.0.1   localhost\n"
            f"127.0.1.1   {hostname}.localdomain {hostname}\n"
            "::1         localhost ip6-localhost ip6-loopback\n"
        )
        hosts_file.write_text(hosts_content)

        # Fix DNS resolution in live environment by ensuring netconfig symlink exists
        resolv_conf = etc_dir / "resolv.conf"
        if not resolv_conf.exists() and not resolv_conf.is_symlink():
            try:
                resolv_conf.symlink_to("/var/run/netconfig/resolv.conf")
            except Exception as e:
                logger.warning(f"Failed to create resolv.conf symlink: {e}")

    def setup_services(self):
        if self.chroot.mode == "mock":
            return
        services = self.config.get("services", [])
        if isinstance(services, dict):
            services = services.get("enable", [])
        services_to_enable = list(services)

        dm = self.config.get("display_manager")
        if not dm:
            if (self.target_root / "usr" / "bin" / "sddm").exists():
                dm = "sddm"
            elif (self.target_root / "usr" / "sbin" / "gdm").exists() or (self.target_root / "usr" / "bin" / "gdm").exists():
                dm = "gdm"
            elif (self.target_root / "usr" / "sbin" / "lightdm").exists() or (self.target_root / "usr" / "bin" / "lightdm").exists():
                dm = "lightdm"

        auto_services = [
            "NetworkManager", "dbus", "avahi-daemon", "smb", "nmb", "sshd", "cups", "polkit",
            "bluetooth", "vboxservice", "qemu-guest-agent", "spice-vdagentd"
        ]
        if dm:
            auto_services.extend(["display-manager"])



        for auto_svc in auto_services:
            if auto_svc not in services_to_enable:
                for search_dir in ["usr/lib/systemd/system", "lib/systemd/system"]:
                    unit = self.target_root / search_dir / f"{auto_svc}.service"
                    if unit.exists():
                        services_to_enable.append(auto_svc)
                        break

        # Set default systemd target to graphical.target for desktop environments
        if self.config.get("desktop") or dm:
            try:
                self.chroot.run_in_chroot(["systemctl", "set-default", "graphical.target"], check=False)
            except Exception:
                pass

            default_target = self.target_root / "etc" / "systemd" / "system" / "default.target"
            default_target.parent.mkdir(parents=True, exist_ok=True)
            if default_target.exists() or default_target.is_symlink():
                default_target.unlink()
            default_target.symlink_to("/usr/lib/systemd/system/graphical.target")

        # Enable services via systemctl and ensure systemd wants symlinks
        graphical_wants = self.target_root / "etc" / "systemd" / "system" / "graphical.target.wants"
        multi_user_wants = self.target_root / "etc" / "systemd" / "system" / "multi-user.target.wants"
        graphical_wants.mkdir(parents=True, exist_ok=True)
        multi_user_wants.mkdir(parents=True, exist_ok=True)

        for svc in services_to_enable:
            try:
                self.chroot.run_in_chroot(["systemctl", "--force", "enable", str(svc)], check=False)
            except Exception:
                pass

            # Create fallback symlinks for graphical target and DM
            for s_dir in ["/usr/lib/systemd/system", "/lib/systemd/system"]:
                s_file = self.target_root / s_dir.lstrip("/") / f"{svc}.service"
                if s_file.exists():
                    wants_link = graphical_wants / f"{svc}.service"
                    if wants_link.exists() or wants_link.is_symlink():
                        wants_link.unlink()
                    try:
                        wants_link.symlink_to(f"{s_dir}/{svc}.service")
                    except Exception:
                        pass
                    break

        if dm:
            sysconfig_dm = self.target_root / "etc" / "sysconfig" / "displaymanager"
            if sysconfig_dm.exists():
                content = sysconfig_dm.read_text()
                # Use a simple regex-like approach to replace DISPLAYMANAGER=""
                import re
                content = re.sub(r'^DISPLAYMANAGER=".*"$', f'DISPLAYMANAGER="{dm}"', content, flags=re.MULTILINE)
                sysconfig_dm.write_text(content)
            else:
                sysconfig_dm.parent.mkdir(parents=True, exist_ok=True)
                sysconfig_dm.write_text(f'DISPLAYMANAGER="{dm}"\n')

    def configure_network_discovery(self):
        """Configure /etc/nsswitch.conf to enable mDNS/Avahi local network discovery."""
        nsswitch = self.target_root / "etc" / "nsswitch.conf"
        if not nsswitch.exists():
            return
            
        content = nsswitch.read_text()
        new_lines = []
        for line in content.splitlines():
            if line.startswith("hosts:") and "mdns" not in line:
                # Add mdns4_minimal before dns for .local resolution
                if "dns" in line:
                    line = line.replace(" dns", " mdns4_minimal [NOTFOUND=return] dns mdns4")
                else:
                    line += " mdns4_minimal [NOTFOUND=return] mdns4"
            new_lines.append(line)
            
        nsswitch.write_text("\n".join(new_lines) + "\n")
        logger.info("Configured nsswitch.conf for Avahi/mDNS network discovery.")

    def _detect_desktop_session(self) -> str:
        # Check actual installed sessions first
        for session_dir in ["usr/share/xsessions", "usr/share/wayland-sessions"]:
            full_dir = self.target_root / session_dir
            if full_dir.exists():
                candidates = [f.stem for f in sorted(full_dir.glob("*.desktop"))]
                if candidates:
                    desktop = str(self.config.get("desktop") or "").lower()
                    for cand in candidates:
                        cand_lower = cand.lower()
                        if desktop and (desktop in cand_lower or cand_lower in desktop):
                            return cand
                    return candidates[0]

        session = self.config.get("desktop_session") or self.config.get("desktop")
        if session:
            session_lower = session.lower()
            if session_lower in {"kde", "plasma"}:
                return "plasma"
            return session_lower
        return "xfce"

    def configure_autologin(self):
        if self.chroot.mode == "mock":
            return
        dm = self.config.get("display_manager")
        live_user = self.config.get("live_user", "liveuser")
        if isinstance(live_user, dict):
            live_user = live_user.get("name", "liveuser")

        # Auto-detect DM if not explicitly specified
        if not dm:
            if (self.target_root / "usr" / "bin" / "sddm").exists():
                dm = "sddm"
            elif (self.target_root / "usr" / "sbin" / "gdm").exists() or (self.target_root / "usr" / "bin" / "gdm").exists():
                dm = "gdm"
            elif (self.target_root / "usr" / "sbin" / "lightdm").exists() or (self.target_root / "usr" / "bin" / "lightdm").exists():
                dm = "lightdm"

        session_name = self._detect_desktop_session()

        # openSUSE native displaymanager sysconfig integration
        sysconfig_dm = self.target_root / "etc" / "sysconfig" / "displaymanager"
        sysconfig_dm.parent.mkdir(parents=True, exist_ok=True)
        sysconfig_content = (
            f'DISPLAYMANAGER="{dm}"\n'
            f'DISPLAYMANAGER_AUTOLOGIN="{live_user}"\n'
            'DISPLAYMANAGER_PASSWORD_LESS_LOGIN="yes"\n'
            'DISPLAYMANAGER_DEFAULT_MODE="x11"\n'
            'DISPLAYMANAGER_REMOTE_ACCESS="no"\n'
            'DISPLAYMANAGER_XSERVER_TCP_PORT_6000_OPEN="no"\n'
        )
        sysconfig_dm.write_text(sysconfig_content)

        # Write Xorg wrapper config to permit non-root Xorg execution across virtualized drivers
        xwrapper = self.target_root / "etc" / "X11" / "Xwrapper.config"
        xwrapper.parent.mkdir(parents=True, exist_ok=True)
        xwrapper.write_text("allowed_users = anybody\nneeds_root_rights = yes\n")

        # Create PAM autologin configuration with proper systemd-logind session integration
        has_common_session = (self.target_root / "etc" / "pam.d" / "common-session").exists()
        has_system_auth = (self.target_root / "etc" / "pam.d" / "system-auth").exists()

        if has_common_session:
            pam_autologin_content = (
                "#%PAM-1.0\n"
                "auth        sufficient  pam_permit.so\n"
                "auth        include     common-auth\n"
                "account     include     common-account\n"
                "password    include     common-password\n"
                "session     required    pam_loginuid.so\n"
                "session     include     common-session\n"
            )
        elif has_system_auth:
            pam_autologin_content = (
                "#%PAM-1.0\n"
                "auth        sufficient  pam_permit.so\n"
                "auth        include     system-auth\n"
                "account     include     system-auth\n"
                "password    include     system-auth\n"
                "session     required    pam_loginuid.so\n"
                "session     include     system-auth\n"
            )
        else:
            pam_autologin_content = (
                "#%PAM-1.0\n"
                "auth        sufficient  pam_permit.so\n"
                "account     sufficient  pam_permit.so\n"
                "password    sufficient  pam_permit.so\n"
                "session     required    pam_loginuid.so\n"
                "session     required    pam_limits.so\n"
                "session     optional    pam_systemd.so\n"
                "session     sufficient  pam_permit.so\n"
            )

        for pam_service in [
            "lightdm-autologin",
            "sddm-autologin",
            "gdm-autologin", "gdm-password",
            "lxdm-autologin",
        ]:
            if not (self.target_root / "etc" / "pam.d" / pam_service).exists() and not (self.target_root / "usr" / "lib" / "pam.d" / pam_service).exists():
                pam_file = self.target_root / "etc" / "pam.d" / pam_service
                pam_file.parent.mkdir(parents=True, exist_ok=True)
                pam_file.write_text(pam_autologin_content)
                try:
                    pam_file.chmod(0o644)
                except Exception:
                    pass

        # Write fallback PAM for standalone DMs if not provided by distro package
        for standalone_dm in ["lightdm", "sddm", "gdm", "gdm3", "lxdm", "slim"]:
            if not (self.target_root / "etc" / "pam.d" / standalone_dm).exists() and not (self.target_root / "usr" / "lib" / "pam.d" / standalone_dm).exists():
                pam_file = self.target_root / "etc" / "pam.d" / standalone_dm
                pam_file.parent.mkdir(parents=True, exist_ok=True)
                pam_file.write_text(pam_autologin_content)
                try:
                    pam_file.chmod(0o644)
                except Exception:
                    pass

        # SDDM configuration
        for sddm_rel in ["etc/sddm.conf.d/autologin.conf", "etc/sddm.conf"]:
            sddm_conf = self.target_root / sddm_rel
            sddm_conf.parent.mkdir(parents=True, exist_ok=True)
            
            # Force X11 for KDE to avoid Wayland black screen bugs in VMs
            if session_name == "plasma" or session_name == "plasma6":
                session_name = "plasmax11"
                
            sddm_conf.write_text(
                f"[Autologin]\nUser={live_user}\nSession={session_name}\nRelogin=false\n"
            )
            
        # Force a beautiful SDDM theme (Breeze) instead of the ugly stock one
        sddm_theme_conf = self.target_root / "etc/sddm.conf.d/theme.conf"
        sddm_theme_conf.parent.mkdir(parents=True, exist_ok=True)
        sddm_theme_conf.write_text("[Theme]\nCurrent=breeze\n")

        # GDM / GDM3 configuration
        gdm_content = (
            "[daemon]\n"
            "WaylandEnable=false\n"
            "AutomaticLoginEnable=true\n"
            f"AutomaticLogin={live_user}\n"
            "TimedLoginEnable=true\n"
            f"TimedLogin={live_user}\n"
            "TimedLoginDelay=0\n"
        )
        for gdm_path in [
            "etc/gdm/custom.conf", "etc/gdm3/custom.conf",
            "etc/gdm/daemon.conf", "etc/gdm3/daemon.conf"
        ]:
            gdm_conf = self.target_root / gdm_path
            gdm_conf.parent.mkdir(parents=True, exist_ok=True)
            gdm_conf.write_text(gdm_content)

        # LightDM configuration
        # LightDM configuration
        if session_name == "cinnamon-wayland" or session_name == "cinnamon2d":
            session_name = "cinnamon"
            
        lightdm_content = (
            "[LightDM]\n"
            "run-directory=/run/lightdm\n\n"
            "[Seat:*]\n"
            "greeter-session=lightdm-gtk-greeter\n"
            f"user-session={session_name}\n"
            "autologin-guest=false\n"
            f"autologin-user={live_user}\n"
            "autologin-user-timeout=0\n"
            "autologin-in-background=false\n"
            f"autologin-session={session_name}\n"
        )
        for conf_rel in ["etc/lightdm/lightdm.conf"]:
            conf_file = self.target_root / conf_rel
            conf_file.parent.mkdir(parents=True, exist_ok=True)
            conf_file.write_text(lightdm_content)

        # Ensure LightDM runtime directories exist and have proper ownership
        for ldir in ["var/lib/lightdm", "var/lib/lightdm-data", "var/log/lightdm", "var/cache/lightdm"]:
            (self.target_root / ldir).mkdir(parents=True, exist_ok=True)
        try:
            self.chroot.run_in_chroot(["chown", "-R", "lightdm:lightdm", "/var/lib/lightdm", "/var/lib/lightdm-data", "/var/log/lightdm", "/var/cache/lightdm"], check=False)
            for g in ["video", "render", "input", "tty", "users"]:
                self.chroot.run_in_chroot(["usermod", "-aG", g, "lightdm"], check=False)
        except Exception:
            pass

        # LXDM configuration
        lxdm_conf = self.target_root / "etc" / "lxdm" / "lxdm.conf"
        if lxdm_conf.parent.exists() or (self.target_root / "usr" / "sbin" / "lxdm").exists():
            lxdm_conf.parent.mkdir(parents=True, exist_ok=True)
            lxdm_conf.write_text(
                f"[base]\nautologin={live_user}\nsession={session_name}\n\n"
                f"[server]\n[display]\n[input]\n"
            )

        # SLiM configuration
        slim_conf = self.target_root / "etc" / "slim.conf"
        if slim_conf.parent.exists() or (self.target_root / "usr" / "bin" / "slim").exists():
            slim_content = (
                f"default_user        {live_user}\n"
                "auto_login          yes\n"
                f"login_cmd           exec /bin/sh - ~/.xinitrc {session_name}\n"
            )
            slim_conf.write_text(slim_content)

        # Greetd (Wayland) configuration
        greetd_conf = self.target_root / "etc" / "greetd" / "config.toml"
        if greetd_conf.parent.exists() or (self.target_root / "usr" / "bin" / "greetd").exists():
            greetd_conf.parent.mkdir(parents=True, exist_ok=True)
            greetd_conf.write_text(
                f"[terminal]\nvt = 1\n\n"
                f"[default_session]\ncommand = \"{session_name}\"\nuser = \"{live_user}\"\n\n"
                f"[initial_session]\ncommand = \"{session_name}\"\nuser = \"{live_user}\"\n"
            )

        # TTY1 Console Autologin (Getty fallback for live session without DM)
        getty_dropin = self.target_root / "etc" / "systemd" / "system" / "getty@tty1.service.d" / "autologin.conf"
        getty_dropin.parent.mkdir(parents=True, exist_ok=True)
        getty_dropin.write_text(
            f"[Service]\nExecStart=\nExecStart=-/sbin/agetty -o '-p -f -- \\\\u' --noclear --autologin {live_user} %I $TERM\n"
        )

    def configure_zram(self):
        if self.chroot.mode == "mock":
            return
        if not self.config.get("with_zram", True):
            return
        zram_conf = self.target_root / "etc" / "systemd" / "zram-generator.conf"
        zram_conf.parent.mkdir(parents=True, exist_ok=True)
        with open(zram_conf, "w") as f:
            f.write("[zram0]\nzram-size = ram / 2\ncompression-algorithm = zstd\n")

    def configure_flathub(self):
        if self.chroot.mode == "mock":
            return
        if not self.config.get("with_flathub", False):
            return
        flatpak_dir = self.target_root / "etc" / "flatpak" / "remotes.d"
        flatpak_dir.mkdir(parents=True, exist_ok=True)
        with open(flatpak_dir / "flathub.flatpakrepo", "w") as f:
            f.write(
                "[Flatpak Remote]\n"
                "Title=Flathub\n"
                "Url=https://dl.flathub.org/repo/\n"
                "GPGKey=https://dl.flathub.org/repo/flathub.gpg\n"
                "Homepage=https://flathub.org/\n"
                "Comment=Central repository of Flatpak applications\n"
            )

    def configure_polkit_power(self):
        if self.chroot.mode == "mock":
            return
        polkit_dir = self.target_root / "etc" / "polkit-1" / "rules.d"
        polkit_dir.mkdir(parents=True, exist_ok=True)
        
        # 1. Allow liveuser full passwordless administrative privileges in live mode
        live_rules = polkit_dir / "00-liveuser.rules"
        live_rules.write_text(
            "/* Allow live user full administrative rights without password in live environment */\n"
            "polkit.addRule(function(action, subject) {\n"
            "    if (subject.isInGroup('wheel') || subject.isInGroup('users') || subject.user === 'liveuser') {\n"
            "        return polkit.Result.YES;\n"
            "    }\n"
            "});\n"
        )

        rule_file = polkit_dir / "10-enable-power-actions.rules"
        rule_content = (
            "polkit.addRule(function(action, subject) {\n"
            "    if (action.id.indexOf('org.freedesktop.login1.') === 0 ||\n"
            "        action.id.indexOf('org.freedesktop.upower.') === 0 ||\n"
            "        action.id.indexOf('org.gnome.SessionManager.') === 0) {\n"
            "        return polkit.Result.YES;\n"
            "    }\n"
            "});\n"
        )
        with open(rule_file, "w") as f:
            f.write(rule_content)

        # 2. Add VirtualBox Guest agent XDG autostart for seamless mouse and clipboard
        autostart_dir = self.target_root / "etc" / "xdg" / "autostart"
        autostart_dir.mkdir(parents=True, exist_ok=True)
        (autostart_dir / "vboxclient.desktop").write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=VirtualBox Guest Agent\n"
            "Exec=/usr/bin/VBoxClient-all\n"
            "Terminal=false\n"
        )

    def configure_calamares(self):
        if self.chroot.mode == "mock":
            return
        if not self.config.get("with_calamares", False):
            return

        # 1. Polkit rule to allow launching Calamares via pkexec without password prompt
        polkit_dir = self.target_root / "etc" / "polkit-1" / "rules.d"
        polkit_dir.mkdir(parents=True, exist_ok=True)
        (polkit_dir / "49-calamares.rules").write_text(
            "/* Allow live user to launch calamares installer via pkexec without password prompt */\n"
            "polkit.addRule(function(action, subject) {\n"
            "    if (action.id === 'org.freedesktop.policykit.exec' && action.lookup('program') === '/usr/bin/calamares') {\n"
            "        return polkit.Result.YES;\n"
            "    }\n"
            "    if (action.id.indexOf('com.github.calamares.') === 0 || action.id.indexOf('io.calamares.') === 0) {\n"
            "        return polkit.Result.YES;\n"
            "    }\n"
            "});\n"
        )

        # 2. Add PolicyKit Action XML for Calamares
        polkit_actions_dir = self.target_root / "usr" / "share" / "polkit-1" / "actions"
        polkit_actions_dir.mkdir(parents=True, exist_ok=True)
        (polkit_actions_dir / "com.github.calamares.calamares.policy").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"\n'
            ' "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">\n'
            '<policyconfig>\n'
            '  <vendor>Calamares</vendor>\n'
            '  <vendor_url>https://calamares.io</vendor_url>\n'
            '  <action id="com.github.calamares.calamares">\n'
            '    <description>Run Calamares Installer</description>\n'
            '    <message>Authentication is required to run the Calamares Installer</message>\n'
            '    <defaults>\n'
            '      <allow_any>yes</allow_any>\n'
            '      <allow_inactive>yes</allow_inactive>\n'
            '      <allow_active>yes</allow_active>\n'
            '    </defaults>\n'
            '    <annotate key="org.freedesktop.policykit.exec.path">/usr/bin/calamares</annotate>\n'
            '    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>\n'
            '  </action>\n'
            '</policyconfig>\n'
        )

        # 2. Add calamares-launcher wrapper script to forward X11 display and credentials cleanly
        launcher_path = self.target_root / "usr" / "local" / "bin" / "calamares-launcher"
        launcher_path.parent.mkdir(parents=True, exist_ok=True)
        launcher_content = (
            "#!/bin/sh\n"
            "# Calamares GUI launcher for live sessions\n"
            "xhost +si:localuser:root >/dev/null 2>&1 || true\n"
            "if [ -z \"$DISPLAY\" ]; then\n"
            "    export DISPLAY=:0\n"
            "fi\n"
            "if [ -z \"$XAUTHORITY\" ] && [ -f \"$HOME/.Xauthority\" ]; then\n"
            "    export XAUTHORITY=\"$HOME/.Xauthority\"\n"
            "fi\n"
            "exec sudo -E /usr/bin/calamares \"$@\"\n"
        )
        launcher_path.write_text(launcher_content)
        launcher_path.chmod(0o755)

        desktop_entry = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            "Name=Install openSUSE\n"
            "Name[pt_PT]=Instalar o openSUSE\n"
            "Comment=Install openSUSE to disk\n"
            "Comment[pt_PT]=Instalar o sistema no disco rígido\n"
            "Exec=/usr/local/bin/calamares-launcher\n"
            "Icon=system-software-install\n"
            "Terminal=false\n"
            "Categories=System;Qt;\n"
            "StartupNotify=true\n"
        )

        # 3. Add launcher to /usr/share/applications/ (and overwrite upstream calamares.desktop)
        apps_dir = self.target_root / "usr" / "share" / "applications"
        apps_dir.mkdir(parents=True, exist_ok=True)
        for app_name in ["install-suse.desktop", "calamares.desktop", "io.calamares.calamares.desktop"]:
            app_desktop = apps_dir / app_name
            app_desktop.write_text(desktop_entry)
            app_desktop.chmod(0o755)

        # 4. Helper script to create and trust desktop icon ONLY for the liveuser
        script_path = self.target_root / "usr" / "local" / "bin" / "add-installer-desktop-icon.sh"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        live_user_name = "liveuser"
        if isinstance(self.config.get("live_user"), dict):
            live_user_name = self.config.get("live_user").get("name", "liveuser")
        elif self.config.get("live_user"):
            live_user_name = str(self.config.get("live_user"))

        script_content = (
            "#!/bin/sh\n"
            "if ! grep -q -E \"(rd\\.live|root=live|boot=live|livecd)\" /proc/cmdline 2>/dev/null; then\n"
            "    exit 0\n"
            "fi\n"
            "sleep 2\n"
            "desktop_dir=$(xdg-user-dir DESKTOP 2>/dev/null)\n"
            "if [ -z \"$desktop_dir\" ]; then\n"
            "    desktop_dir=\"$HOME/Desktop\"\n"
            "fi\n"
            "mkdir -p \"$desktop_dir\"\n"
            "rm -f \"$desktop_dir/calamares.desktop\" \"$desktop_dir/io.calamares.calamares.desktop\"\n"
            "icon_path=\"$desktop_dir/install-suse.desktop\"\n"
            "cat << 'EOF' > \"$icon_path\"\n"
            f"{desktop_entry}"
            "EOF\n"
            "chmod +x \"$icon_path\"\n"
            "if command -v gio >/dev/null 2>&1; then\n"
            "    gio set --type=string \"$icon_path\" metadata::trusted true 2>/dev/null\n"
            "    if command -v sha256sum >/dev/null 2>&1; then\n"
            "        checksum=$(sha256sum \"$icon_path\" | cut -d' ' -f1)\n"
            "        gio set --type=string \"$icon_path\" metadata::xfce-exe-checksum \"$checksum\" 2>/dev/null\n"
            "    fi\n"
            "fi\n"
            "touch \"$icon_path\"\n"
        )
        script_path.write_text(script_content)
        script_path.chmod(0o755)

        # 5. Autostart desktop entry (/etc/xdg/autostart/)
        autostart_dir = self.target_root / "etc" / "xdg" / "autostart"
        autostart_dir.mkdir(parents=True, exist_ok=True)
        (autostart_dir / "create-install-icon.desktop").write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Create Install Icon\n"
            "Exec=/usr/local/bin/add-installer-desktop-icon.sh\n"
            "Icon=system-software-install\n"
            "Terminal=false\n"
            "NoDisplay=true\n"
        )

        # 6. Also install for existing users in /home/*
        home_dir = self.target_root / "home"
        if home_dir.exists():
            for user_dir in home_dir.iterdir():
                if user_dir.is_dir():
                    user_desktop = user_dir / "Desktop" / "install-suse.desktop"
                    user_desktop.parent.mkdir(parents=True, exist_ok=True)
                    user_desktop.write_text(desktop_entry)
                    user_desktop.chmod(0o755)

    def configure_locales(self):
        locale = self.config.get("system", {}).get("locale", "en_US.UTF-8")
        locale_gen = self.target_root / "etc" / "locale.gen"
        if locale_gen.exists():
            content = locale_gen.read_text()
            for loc in [f"{locale} UTF-8"]:
                if f"# {loc}" in content:
                    content = content.replace(f"# {loc}", loc)
                elif loc not in content:
                    content += f"\n{loc}\n"
            locale_gen.write_text(content)
        else:
            locale_gen.parent.mkdir(parents=True, exist_ok=True)
            locale_gen.write_text(f"{locale} UTF-8\n")

        locale_conf = self.target_root / "etc" / "locale.conf"
        locale_conf.parent.mkdir(parents=True, exist_ok=True)
        locale_conf.write_text(f"LANG={locale}\nLC_ALL={locale}\n")

        sysconfig_lang = self.target_root / "etc" / "sysconfig" / "language"
        if sysconfig_lang.exists():
            content = sysconfig_lang.read_text()
            import re
            content = re.sub(r'^RC_LANG=.*$', f'RC_LANG="{locale}"', content, flags=re.MULTILINE)
            content = re.sub(r'^RC_LC_ALL=.*$', f'RC_LC_ALL="{locale}"', content, flags=re.MULTILINE)
            sysconfig_lang.write_text(content)
        else:
            sysconfig_lang.write_text(f'RC_LANG="{locale}"\nRC_LC_ALL="{locale}"\n')

        logger.info(f"Locale configuration written: {locale}")

    def fix_home_permissions(self):
        if self.chroot.mode == "mock":
            return
        live_user_cfg = self.config.get("live_user", "liveuser")
        if isinstance(live_user_cfg, dict):
            live_user = live_user_cfg.get("name", "liveuser")
        else:
            live_user = str(live_user_cfg)

        user_home = self.target_root / "home" / live_user
        if user_home.exists():
            self.chroot.run_in_chroot(["chown", "-R", f"{live_user}:{live_user}", f"/home/{live_user}"], check=False)
            self.chroot.run_in_chroot(["chmod", "0755", f"/home/{live_user}"], check=False)

    def configure_branding(self):
        if self.chroot.mode == "mock":
            return
        etc_dir = self.target_root / "etc"
        etc_dir.mkdir(parents=True, exist_ok=True)

        os_release = etc_dir / "os-release"
        if not os_release.exists():
            os_release.write_text(
                'NAME="openSUSE Modern"\n'
                'ID="opensuse_modern"\n'
                'ID_LIKE="opensuse suse"\n'
                'PRETTY_NAME="openSUSE Modern GNU/Linux"\n'
                'VERSION="2026.08"\n'
                'VERSION_ID="2026.08"\n'
                'HOME_URL="https://github.com/acoroslinux/suse-builder"\n'
                'SUPPORT_URL="https://github.com/acoroslinux/suse-builder"\n'
                'BUG_REPORT_URL="https://github.com/acoroslinux/suse-builder"\n'
                'LOGO="distributor-logo-opensuse"\n'
            )

        issue_file = etc_dir / "issue"
        issue_file.write_text("openSUSE Modern GNU/Linux \\r (\\l)\n\n")

        rel_file = etc_dir / "opensuse_modern-release"
        rel_file.write_text("openSUSE Modern release 2026.08\n")

        # Configure Plymouth daemon default theme
        plymouth_dir = etc_dir / "plymouth"
        plymouth_dir.mkdir(parents=True, exist_ok=True)
        (plymouth_dir / "plymouthd.conf").write_text(
            "[Daemon]\n"
            "Theme=suse-modern\n"
            "ShowDelay=0\n"
            "DeviceTimeout=8\n"
        )

        # Copy suse-modern plymouth theme to /usr/share/plymouth/themes/
        from suse_builder.core.path_utils import resolve_from_project
        plymouth_src = resolve_from_project("configs/custom_files/plymouth/themes/suse-modern")
        if plymouth_src.exists():
            import shutil
            plymouth_dst = self.target_root / "usr" / "share" / "plymouth" / "themes" / "suse-modern"
            plymouth_dst.mkdir(parents=True, exist_ok=True)
            for f in plymouth_src.iterdir():
                if f.is_file():
                    shutil.copy2(f, plymouth_dst / f.name)
            
            # Set default.plymouth symlink
            def_ply = self.target_root / "usr" / "share" / "plymouth" / "themes" / "default.plymouth"
            if def_ply.exists() or def_ply.is_symlink():
                def_ply.unlink()
            def_ply.symlink_to(Path("suse-modern/suse-modern.plymouth"))

    def configure_dracut(self):
        if self.chroot.mode == "mock":
            return
        dracut_conf_dir = self.target_root / "etc" / "dracut.conf.d"
        dracut_conf_dir.mkdir(parents=True, exist_ok=True)
        live_conf = dracut_conf_dir / "02-live.conf"
        live_conf.write_text(
            '# Added by suse-builder for Live ISO generation\n'
            'add_dracutmodules+=" dmsquash-live pollcdrom qemu qemu-net base rootfs-block udev-rules kernel-modules plymouth drm "\n'
            'omit_dracutmodules+=" checkisomd5 "\n'
            'compress="xz"\n'
            'add_drivers+=" squashfs loop overlay iso9660 isofs zstd zstd_decompress dm_mod sr_mod cdrom sd_mod ahci ata_piix ata_generic pata_acpi pata_serverworks virtio_blk virtio_scsi virtio_pci virtio_net uas usb_storage nvme bochs-drm vmwgfx virtio-gpu qxl nouveau radeon amdgpu i915 "\n'
            'filesystems+=" squashfs iso9660 overlay vfat ext4 "\n'
            'hostonly="no"\n'
        )
        # Add systemd condition to checkisomd5 so it ONLY runs if requested
        logger.info("Adding ConditionKernelCommandLine to checkisomd5@.service")
        dropin_dir = self.target_root / "etc" / "systemd" / "system" / "checkisomd5@.service.d"
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin_file = dropin_dir / "condition.conf"
        dropin_file.write_text("[Unit]\nConditionKernelCommandLine=rd.live.check=1\n")

        logger.info("Dracut live configuration written to /etc/dracut.conf.d/02-live.conf.")

    def configure_live_environment(self):
        self.configure_locales()
        self.setup_live_users()
        self.configure_system_defaults()
        self.configure_branding()
        self.configure_network_discovery()
        self.setup_services()
        self.configure_gnome_defaults()
        self.configure_kde_defaults()
        self.configure_autologin()
        self.configure_zram()
        self.configure_flathub()
        self.configure_fonts()
        self.configure_polkit_power()
        self.configure_calamares()
        self.configure_artwork()
        self.copy_custom_files()
        if self.config.get("with_offline_repo") or self.config.get("offline_repo_packages"):
            self.configure_offline_repository()
        self.configure_dracut()
        self.configure_machine_id()
        self.fix_system_permissions()

    def configure_fonts(self):
        """Configure fontconfig for crisp text rendering, subpixel RGB, and proper monospace aliases."""
        if self.chroot.mode == "mock":
            return
        fonts_dir = self.target_root / "etc" / "fonts"
        fonts_dir.mkdir(parents=True, exist_ok=True)
        local_conf = fonts_dir / "local.conf"
        local_conf_content = (
            '<?xml version="1.0"?>\n'
            '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
            '<fontconfig>\n'
            '  <!-- Global rendering properties -->\n'
            '  <match target="font">\n'
            '    <edit mode="assign" name="antialias"><bool>true</bool></edit>\n'
            '    <edit mode="assign" name="hinting"><bool>true</bool></edit>\n'
            '    <edit mode="assign" name="hintstyle"><const>hintslight</const></edit>\n'
            '    <edit mode="assign" name="rgba"><const>rgb</const></edit>\n'
            '    <edit mode="assign" name="lcdfilter"><const>lcddefault</const></edit>\n'
            '  </match>\n'
            '\n'
            '  <!-- Default font mappings -->\n'
            '  <alias>\n'
            '    <family>sans-serif</family>\n'
            '    <prefer>\n'
            '      <family>Noto Sans</family>\n'
            '      <family>DejaVu Sans</family>\n'
            '      <family>Cantarell</family>\n'
            '    </prefer>\n'
            '  </alias>\n'
            '  <alias>\n'
            '    <family>serif</family>\n'
            '    <prefer>\n'
            '      <family>Noto Serif</family>\n'
            '      <family>DejaVu Serif</family>\n'
            '    </prefer>\n'
            '  </alias>\n'
            '  <alias>\n'
            '    <family>monospace</family>\n'
            '    <prefer>\n'
            '      <family>Hack</family>\n'
            '      <family>JetBrains Mono</family>\n'
            '      <family>Fira Code</family>\n'
            '      <family>DejaVu Sans Mono</family>\n'
            '      <family>Liberation Mono</family>\n'
            '      <family>Noto Sans Mono</family>\n'
            '    </prefer>\n'
            '  </alias>\n'
            '</fontconfig>\n'
        )
        local_conf.write_text(local_conf_content)
        
        # Configure default monospace font for GNOME and Cinnamon desktop interfaces
        schema_dir = self.target_root / "usr" / "share" / "glib-2.0" / "schemas"
        schema_dir.mkdir(parents=True, exist_ok=True)
        fonts_override = (
            "[org.gnome.desktop.interface]\n"
            "monospace-font-name='Hack 10'\n"
            "font-name='Noto Sans 10'\n"
            "\n"
            "[org.cinnamon.desktop.interface]\n"
            "monospace-font-name='Hack 10'\n"
            "font-name='Noto Sans 10'\n"
        )
        (schema_dir / "99_opensuse_modern_fonts.gschema.override").write_text(fonts_override)
        try:
            self.chroot.run_in_chroot(["glib-compile-schemas", "/usr/share/glib-2.0/schemas/"], check=False)
        except Exception:
            pass

        logger.info("Configured system font rendering and aliases in /etc/fonts/local.conf.")

    def configure_offline_repository(self):
        """Configure /etc/zypp/repos.d/offline-iso.repo pointing to the ISO's offline repository."""
        if self.chroot.mode == "mock":
            return
        repos_d = self.target_root / "etc" / "zypp" / "repos.d"
        repos_d.mkdir(parents=True, exist_ok=True)
        vol_id = self.config.get("iso_label") or self.config.get("volume_id", "OPENSUSE_MODERN")
        repo_content = (
            "[offline-iso]\n"
            "name=openSUSE Offline ISO Repository\n"
            "enabled=1\n"
            "autorefresh=0\n"
            f"baseurl=cd:/?devices=/dev/disk/by-label/{vol_id}&path=/repo/x86_64\n"
            "gpgcheck=0\n"
            "keeppackages=0\n"
            "type=rpm-md\n"
            "\n"
            "[offline-iso-fallback]\n"
            "name=openSUSE Offline ISO Fallback (Mount Directory)\n"
            "enabled=1\n"
            "autorefresh=0\n"
            f"baseurl=dir:/run/media/liveuser/{vol_id}/repo/x86_64\n"
            "gpgcheck=0\n"
            "keeppackages=0\n"
            "type=rpm-md\n"
        )
        (repos_d / "offline-iso.repo").write_text(repo_content)
        logger.info("Configured openSUSE offline ISO repository in /etc/zypp/repos.d/offline-iso.repo")

    def fix_home_permissions(self):
        self.fix_system_permissions()

    def fix_system_permissions(self):
        """Fix permissions and ownership on critical system files, shadow, PAM, sudoers, and home."""
        if self.chroot.mode == "mock":
            return

        live_user = self.config.get("live_user", "liveuser")
        if isinstance(live_user, dict):
            live_user = live_user.get("name", "liveuser")

        # 1. Shadow and password databases
        for f, perm in [
            ("etc/passwd", 0o644),
            ("etc/group", 0o644),
            ("etc/shadow", 0o640),
            ("etc/gshadow", 0o640),
            ("etc/shadow-", 0o600),
            ("etc/gshadow-", 0o600),
            ("etc/subuid", 0o644),
            ("etc/subgid", 0o644),
            ("etc/sudoers", 0o440),
        ]:
            p = self.target_root / f
            if p.exists():
                try:
                    p.chmod(perm)
                except Exception:
                    pass

        # 2. Sudoers.d drop-in permissions
        sudoers_d = self.target_root / "etc" / "sudoers.d"
        if sudoers_d.is_dir():
            try:
                sudoers_d.chmod(0o750)
                for sf in sudoers_d.iterdir():
                    if sf.is_file():
                        sf.chmod(0o440)
            except Exception:
                pass
        
        # 3. Ensure root ownership for critical security files (shadow, sudoers, etc.)
        for path in ["/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/sudoers.d"]:
            self.chroot.run_in_chroot(["chown", "-R", "root:root", path], check=False)

        # 3. PAM directory and files
        pamd = self.target_root / "etc" / "pam.d"
        if pamd.is_dir():
            try:
                pamd.chmod(0o755)
                for pf in pamd.iterdir():
                    if pf.is_file():
                        pf.chmod(0o644)
            except Exception:
                pass

        # 4. Polkit rules permissions
        polkit_d = self.target_root / "etc" / "polkit-1" / "rules.d"
        if polkit_d.is_dir():
            try:
                polkit_d.chmod(0o755)
                for rf in polkit_d.iterdir():
                    if rf.is_file():
                        rf.chmod(0o644)
            except Exception:
                pass

        # 5. Setuid binaries for sudo / su / pkexec
        for suid_bin in ["usr/bin/sudo", "usr/bin/su", "usr/bin/pkexec", "usr/bin/newuidmap", "usr/bin/newgidmap"]:
            bp = self.target_root / suid_bin
            if bp.exists():
                try:
                    self.chroot.run_in_chroot(["chown", "root:root", f"/{suid_bin}"], check=False)
                    self.chroot.run_in_chroot(["chmod", "4755", f"/{suid_bin}"], check=False)
                except Exception:
                    pass

        # 6. Display manager runtime directories
        for dm_dir in ["var/lib/lightdm", "var/lib/lightdm-data", "var/log/lightdm", "var/cache/lightdm"]:
            dp = self.target_root / dm_dir
            if dp.exists():
                try:
                    self.chroot.run_in_chroot(["chown", "-R", "lightdm:lightdm", f"/{dm_dir}"], check=False)
                    self.chroot.run_in_chroot(["chmod", "775", f"/{dm_dir}"], check=False)
                except Exception:
                    pass

        for sddm_dir in ["var/lib/sddm"]:
            dp = self.target_root / sddm_dir
            if dp.exists():
                try:
                    self.chroot.run_in_chroot(["chown", "-R", "sddm:sddm", f"/{sddm_dir}"], check=False)
                except Exception:
                    pass

        # 7. User home directory
        home_dir = self.target_root / "home" / live_user
        if home_dir.exists():
            try:
                self.chroot.run_in_chroot(["chown", "-R", f"{live_user}:{live_user}", f"/home/{live_user}"], check=False)
                self.chroot.run_in_chroot(["chmod", "755", f"/home/{live_user}"], check=False)
            except Exception as e:
                logger.warning(f"Could not fix permissions on /home/{live_user}: {e}")

        # 8. Temporary directories sticky bit
        for tmp_path in ["/tmp", "/var/tmp"]:
            try:
                self.chroot.run_in_chroot(["chmod", "1777", tmp_path], check=False)
            except Exception:
                pass

        logger.info("Fixed system permissions on shadow, PAM, sudoers, display manager, and user home.")

    def configure_machine_id(self):
        machine_id_path = self.chroot.target_root / "etc" / "machine-id"
        machine_id_path.parent.mkdir(parents=True, exist_ok=True)
        machine_id_path.write_text("")  # Empty = transient live ID
        logger.info("Set /etc/machine-id to empty (transient live mode).")

        dbus_machine_id = self.chroot.target_root / "var" / "lib" / "dbus" / "machine-id"
        dbus_machine_id.parent.mkdir(parents=True, exist_ok=True)
        if not dbus_machine_id.is_symlink():
            if dbus_machine_id.exists():
                dbus_machine_id.unlink()
            try:
                dbus_machine_id.symlink_to("/etc/machine-id")
            except Exception:
                dbus_machine_id.write_text("")

        # Mask systemd-machine-id-commit to prevent re-commit on read-only rootfs
        systemd_dir = self.chroot.target_root / "etc" / "systemd" / "system"
        systemd_dir.mkdir(parents=True, exist_ok=True)
        commit_mask = systemd_dir / "systemd-machine-id-commit.service"
        if not commit_mask.exists():
            commit_mask.symlink_to("/dev/null")

        # Write minimal /etc/fstab if missing
        fstab = self.chroot.target_root / "etc" / "fstab"
        if not fstab.exists() or fstab.stat().st_size == 0:
            fstab.parent.mkdir(parents=True, exist_ok=True)
            fstab.write_text(
                "# /etc/fstab: static file system information.\n"
                "# <file system>  <mount point>  <type>  <options>  <dump>  <pass>\n"
                "tmpfs  /tmp  tmpfs  defaults,noatime,mode=1777  0  0\n"
                "tmpfs  /run  tmpfs  defaults,noatime,mode=0755  0  0\n"
            )

        # Purge SSH host keys (regenerated on first real boot)
        for key_file in (self.chroot.target_root / "etc" / "ssh").glob("ssh_host_*"):
            key_file.unlink(missing_ok=True)

        logger.info("machine-id and fstab configured for live environment.")

    def copy_custom_files(self):
        """
        Copies custom files and overlays into the target rootfs chroot.
        Supports both:
        1. Direct rootfs overlay from configs/custom_files/ -> /
        2. Structured JSON custom_files / copy_files entries mapping source -> destination.
        """
        if self.chroot.mode == "mock":
            return

        import shutil
        from suse_builder.core.path_utils import resolve_from_project
        project_root = resolve_from_project("")
        custom_files_dir = project_root / "configs" / "custom_files"

        # 1. Direct overlay from configs/custom_files/ has been moved to BrandingManager
        # to prevent polluting the root directory with flat folder names.
        rsync_log_path = self.target_root.parent.parent / "custom_files_copy.log"
        if rsync_log_path.exists():
            rsync_log_path.unlink()

        # 2. Structured list from JSON config
        custom_files_list = list(self.config.get("custom_files", []))
        base_copy_files = self.config.get("base_copy_files", [])
        if isinstance(base_copy_files, list):
            for entry in base_copy_files:
                if entry not in custom_files_list:
                    custom_files_list.append(entry)

        copy_files = self.config.get("copy_files", [])
        if isinstance(copy_files, list):
            for entry in copy_files:
                if entry not in custom_files_list:
                    custom_files_list.append(entry)

        desktop_env = self.config.get("desktop_environment", {})
        if isinstance(desktop_env, dict):
            for entry in desktop_env.get("copy_files", []):
                if entry not in custom_files_list:
                    custom_files_list.append(entry)

        if not custom_files_list:
            return

        py_ver = "3.12"
        python_dirs = list(self.target_root.glob("usr/lib/python3.*"))
        if python_dirs:
            py_ver = python_dirs[0].name.replace("python", "")

        for entry in custom_files_list:
            if not isinstance(entry, dict):
                continue
            src_rel = entry.get("source")
            dest_rel = entry.get("destination")
            if not src_rel or not dest_rel:
                continue

            dest_rel = dest_rel.format(python_version=py_ver)
            src_path = custom_files_dir / src_rel
            if not src_path.exists():
                src_path = project_root / src_rel
            dest_path = self.target_root / dest_rel.lstrip("/")

            if not src_path.exists():
                continue

            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with open(rsync_log_path, "a") as rsync_log:
                if src_path.is_dir():
                    dest_path.mkdir(parents=True, exist_ok=True)
                    proc = subprocess.run(["rsync", "-av", "--no-o", "--no-g", "--force", f"{src_path}/", f"{dest_path}/"], stdout=rsync_log, stderr=subprocess.STDOUT, check=False)
                else:
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    proc = subprocess.run(["rsync", "-av", "--no-o", "--no-g", "--force", str(src_path), str(dest_path)], stdout=rsync_log, stderr=subprocess.STDOUT, check=False)

            if proc.returncode != 0 or not dest_path.exists():
                logger.error(f"Failed to copy structured custom file/dir: {src_path} -> {dest_path}. Check {rsync_log_path} for details.")
                raise RuntimeError(f"Structured custom files validation failed for {src_rel}")

            mode_str = entry.get("permissions")
            if mode_str:
                try:
                    mode = int(mode_str, 8)
                    dest_path.chmod(mode)
                except Exception:
                    pass

    def configure_artwork(self):
        """Install custom openSUSE Modern artwork and set default wallpaper link."""
        if self.chroot.mode == "mock":
            return
        bg_dir = self.target_root / "usr" / "share" / "backgrounds"
        bg_dir.mkdir(parents=True, exist_ok=True)

        from suse_builder.core.path_utils import resolve_from_project
        
        grub_bg_src = resolve_from_project("configs/custom_files/boot/grub2/themes/suse-modern/suse-grub-bg.jpg")
        
        # Ensure the symlink exists so grub2-mkconfig on the installed system finds the theme
        boot_themes = self.target_root / "boot" / "grub2" / "themes"
        boot_themes.mkdir(parents=True, exist_ok=True)
        boot_theme_link = boot_themes / "openSUSE"
        if not boot_theme_link.exists() and not boot_theme_link.is_symlink():
            boot_theme_link.symlink_to("/usr/share/grub2/themes/openSUSE")

        custom_wp = resolve_from_project("configs/custom_files/backgrounds/suse-modern-wallpaper.png")
        if custom_wp.exists():
            import shutil
            dest_wp = bg_dir / "suse-modern-wallpaper.png"
            shutil.copy2(custom_wp, dest_wp)

            default_wp = bg_dir / "default-wallpaper.png"
            if default_wp.exists() or default_wp.is_symlink():
                default_wp.unlink()
            default_wp.symlink_to(Path("/usr/share/backgrounds/suse-modern-wallpaper.png"))

        # Configure default XFCE desktop wallpaper via xfconf template
        xfconf_dir = self.target_root / "etc" / "skel" / ".config" / "xfce4" / "xfconf" / "xfce-perchannel-xml"
        xfconf_dir.mkdir(parents=True, exist_ok=True)
        xfce_desktop_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<channel name="xfce4-desktop" version="1.0">\n'
            '  <property name="backdrop" type="empty">\n'
            '    <property name="screen0" type="empty">\n'
            '      <property name="monitor0" type="empty">\n'
            '        <property name="workspace0" type="empty">\n'
            '          <property name="color-style" type="int" value="0"/>\n'
            '          <property name="image-style" type="int" value="5"/>\n'
            '          <property name="last-image" type="string" value="/usr/share/backgrounds/default-wallpaper.png"/>\n'
            '        </property>\n'
            '      </property>\n'
            '    </property>\n'
            '  </property>\n'
            '</channel>\n'
        )
        (xfconf_dir / "xfce4-desktop.xml").write_text(xfce_desktop_xml)

        # Configure KDE default wallpaper if present
        # In openSUSE, KDE Plasma defaults to the openSUSEdefault wallpaper
        kde_wp_dir = self.target_root / "usr" / "share" / "wallpapers" / "openSUSEdefault" / "contents" / "images"
        if kde_wp_dir.exists() and custom_wp.exists():
            default_kde_wp = kde_wp_dir / "default.png"
            default_dark_kde_wp = kde_wp_dir / "default-dark.png"
            # Overwrite the default PNGs with our custom wallpaper
            import shutil
            shutil.copy2(custom_wp, default_kde_wp)
            shutil.copy2(custom_wp, default_dark_kde_wp)

    def configure_gnome_defaults(self):
        if self.chroot.mode == "mock" or self.config.get("desktop") != "gnome":
            return
        
        logger.info("Configuring custom GNOME defaults (extensions, themes, dock)...")
        schema_dir = self.target_root / "usr" / "share" / "glib-2.0" / "schemas"
        schema_dir.mkdir(parents=True, exist_ok=True)
        
        override_content = (
            "[org.gnome.shell]\n"
            "enabled-extensions=['dash-to-dock@micxgx.gmail.com', 'appindicatorsupport@rgcjonas.gmail.com']\n"
            "\n"
            "[org.gnome.desktop.interface]\n"
            "icon-theme='Papirus'\n"
            "color-scheme='prefer-dark'\n"
            "\n"
            "[org.gnome.mutter]\n"
            "center-new-windows=true\n"
            "\n"
            "[org.gnome.shell.extensions.dash-to-dock]\n"
            "dock-position='BOTTOM'\n"
            "dash-max-icon-size=48\n"
            "click-action='minimize'\n"
            "show-trash=false\n"
            "show-mounts=false\n"
        )
        
        (schema_dir / "99_opensuse_modern.gschema.override").write_text(override_content)
        
        # Recompile schemas inside chroot
        try:
            self.chroot.run_in_chroot(["glib-compile-schemas", "/usr/share/glib-2.0/schemas/"], check=False)
        except Exception as e:
            logger.warning(f"Failed to compile GNOME schemas: {e}")

    def configure_kde_defaults(self):
        if self.chroot.mode == "mock" or self.config.get("desktop") != "kde":
            return
            
        logger.info("Configuring custom KDE Plasma 6 defaults (Breeze Dark, Wallpaper)...")
        skel_config = self.target_root / "etc" / "skel" / ".config"
        skel_config.mkdir(parents=True, exist_ok=True)
        
        # Force Breeze Dark theme globally
        kdeglobals = skel_config / "kdeglobals"
        kdeglobals_content = (
            "[KDE]\n"
            "LookAndFeelPackage=org.kde.breezedark.desktop\n"
            "\n"
            "[General]\n"
            "ColorScheme=BreezeDark\n"
        )
        kdeglobals.write_text(kdeglobals_content)
        
        # Disable screen locking in Live ISO
        kscreenlockerrc = skel_config / "kscreenlockerrc"
        kscreenlockerrc.write_text(
            "[Daemon]\n"
            "Autolock=false\n"
            "LockOnResume=false\n"
        )

    def fix_home_permissions(self):
        if self.chroot.mode == "mock":
            return
        live_user = self.config.get("live_user", "liveuser")
        if isinstance(live_user, dict):
            live_user = live_user.get("name", "liveuser")

        home_dir = self.target_root / "home" / live_user
        if home_dir.exists():
            try:
                self.chroot.run_in_chroot(["chown", "-R", f"{live_user}:{live_user}", f"/home/{live_user}"], check=False)
                self.chroot.run_in_chroot(["chmod", "755", f"/home/{live_user}"], check=False)
            except Exception as e:
                logger.warning(f"Could not fix permissions on /home/{live_user}: {e}")

        # Ensure sticky bit on /tmp and /var/tmp
        for tmp_path in ["/tmp", "/var/tmp"]:
            try:
                self.chroot.run_in_chroot(["chmod", "1777", tmp_path], check=False)
            except Exception:
                pass
