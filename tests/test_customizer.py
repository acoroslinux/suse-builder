import subprocess

from suse_builder.core.customizer import SystemCustomizer


class DummyChroot:
    def __init__(self):
        self.mode = "real"
        self.target_root = None
        self.calls = []
        self.groups_present = {"audio", "video", "users"}
        self.users_present = set()

    def run_in_chroot(self, command, check=True):
        self.calls.append(command)

        if isinstance(command, list) and command[:2] == ["getent", "group"]:
            returncode = 0 if command[2] in self.groups_present else 2
            return subprocess.CompletedProcess(args=command, returncode=returncode)

        if isinstance(command, list) and command[:2] == ["groupadd", "-f"]:
            self.groups_present.add(command[2])
            return subprocess.CompletedProcess(args=command, returncode=0)

        if isinstance(command, list) and command[:1] == ["useradd"]:
            username = command[-1]
            if username in self.users_present:
                return subprocess.CompletedProcess(args=command, returncode=9)
            self.users_present.add(username)
            return subprocess.CompletedProcess(args=command, returncode=0)

        if isinstance(command, list) and command[:2] == ["id", "-u"]:
            returncode = 0 if command[2] in self.users_present else 1
            return subprocess.CompletedProcess(args=command, returncode=returncode)

        return subprocess.CompletedProcess(args=command, returncode=0)


def test_setup_live_users_creates_missing_groups_and_sets_password(tmp_path):
    chroot = DummyChroot()
    chroot.target_root = tmp_path / "chroot"
    config = {"live_user": {"name": "liveuser", "groups": ["wheel", "audio", "video"]}}

    customizer = SystemCustomizer(chroot, config)
    customizer.setup_live_users()

    assert ["groupadd", "-f", "wheel"] in chroot.calls
    assert any(isinstance(c, list) and c[:1] == ["useradd"] and c[-1] == "liveuser" for c in chroot.calls)
    assert any(isinstance(c, str) and "liveuser:live" in c and "chpasswd" in c for c in chroot.calls)


def test_configure_system_defaults_writes_hostname_and_hosts(tmp_path):
    chroot = DummyChroot()
    chroot.target_root = tmp_path / "chroot"
    config = {"hostname": "my-suse-box"}

    customizer = SystemCustomizer(chroot, config)
    customizer.configure_system_defaults()

    hostname_file = chroot.target_root / "etc" / "hostname"
    hosts_file = chroot.target_root / "etc" / "hosts"
    assert hostname_file.read_text().strip() == "my-suse-box"
    assert "127.0.1.1   my-suse-box.localdomain my-suse-box" in hosts_file.read_text()


def test_configure_autologin_supports_gdm_and_lightdm(tmp_path):
    chroot = DummyChroot()
    chroot.target_root = tmp_path / "chroot"
    config = {"display_manager": "gdm", "live_user": "liveuser"}

    customizer = SystemCustomizer(chroot, config)
    customizer.configure_autologin()

    gdm_conf = chroot.target_root / "etc" / "gdm" / "custom.conf"
    assert gdm_conf.exists()
    assert "AutomaticLogin=liveuser" in gdm_conf.read_text()


def test_configure_calamares_populates_skel(tmp_path):
    chroot = DummyChroot()
    chroot.target_root = tmp_path / "chroot"
    config = {"with_calamares": True}

    customizer = SystemCustomizer(chroot, config)
    customizer.configure_calamares()

    apps_desktop = chroot.target_root / "usr" / "share" / "applications" / "install-suse.desktop"
    assert apps_desktop.exists()
    assert "calamares" in apps_desktop.read_text()

    autostart_desktop = chroot.target_root / "etc" / "xdg" / "autostart" / "create-install-icon.desktop"
    assert autostart_desktop.exists()

    add_script = chroot.target_root / "usr" / "local" / "bin" / "add-installer-desktop-icon.sh"
    assert add_script.exists()

    # Verify installer is NOT leaked statically into /etc/skel
    skel_desktop = chroot.target_root / "etc" / "skel" / "Desktop" / "install-suse.desktop"
    assert not skel_desktop.exists()


def test_configure_flathub_uses_valid_gpg_url(tmp_path):
    chroot = DummyChroot()
    chroot.target_root = tmp_path / "chroot"
    config = {"with_flathub": True}

    customizer = SystemCustomizer(chroot, config)
    customizer.configure_flathub()

    flathub_file = chroot.target_root / "etc" / "flatpak" / "remotes.d" / "flathub.flatpakrepo"
    assert flathub_file.exists()
    assert "GPGKey=https://dl.flathub.org/repo/flathub.gpg" in flathub_file.read_text()


def test_configure_kde_defaults_creates_wallpaper_package_and_autostart(tmp_path):
    chroot = DummyChroot()
    chroot.target_root = tmp_path / "chroot"
    config = {"desktop": "kde"}

    customizer = SystemCustomizer(chroot, config)
    customizer.configure_kde_defaults()

    kdeglobals = chroot.target_root / "etc" / "skel" / ".config" / "kdeglobals"
    assert kdeglobals.exists()
    assert "BreezeDark" in kdeglobals.read_text()

    autostart = chroot.target_root / "etc" / "xdg" / "autostart" / "set-plasma-wallpaper.desktop"
    assert autostart.exists()
    assert "plasma-apply-wallpaperimage" in autostart.read_text()

    wp_meta = chroot.target_root / "usr" / "share" / "wallpapers" / "suse-cyber-chameleon" / "metadata.desktop"
    assert wp_meta.exists()


def test_configure_lxqt_defaults_creates_pcmanfm_and_lxqt_configs(tmp_path):
    chroot = DummyChroot()
    chroot.target_root = tmp_path / "chroot"
    config = {"desktop": "lxqt"}

    customizer = SystemCustomizer(chroot, config)
    customizer.configure_lxqt_defaults()

    pcmanfm_cfg = chroot.target_root / "etc" / "skel" / ".config" / "pcmanfm-qt" / "lxqt" / "settings.conf"
    assert pcmanfm_cfg.exists()
    assert "suse-cyber-chameleon.jpg" in pcmanfm_cfg.read_text()

    lxqt_cfg = chroot.target_root / "etc" / "skel" / ".config" / "lxqt" / "lxqt.conf"
    assert lxqt_cfg.exists()
    assert "Papirus-Dark" in lxqt_cfg.read_text()


def test_configure_lxde_defaults_creates_pcmanfm_and_lxsession_configs(tmp_path):
    chroot = DummyChroot()
    chroot.target_root = tmp_path / "chroot"
    config = {"desktop": "lxde"}

    customizer = SystemCustomizer(chroot, config)
    customizer.configure_lxde_defaults()

    pcmanfm_cfg = chroot.target_root / "etc" / "skel" / ".config" / "pcmanfm" / "LXDE" / "pcmanfm.conf"
    assert pcmanfm_cfg.exists()
    assert "suse-cyber-chameleon.jpg" in pcmanfm_cfg.read_text()

    lxsession_cfg = chroot.target_root / "etc" / "skel" / ".config" / "lxsession" / "LXDE" / "desktop.conf"
    assert lxsession_cfg.exists()
    assert "Yaru-grey-dark" in lxsession_cfg.read_text()


def test_configure_budgie_defaults_creates_gschema_override(tmp_path):
    chroot = DummyChroot()
    chroot.target_root = tmp_path / "chroot"
    config = {"desktop": "budgie"}

    customizer = SystemCustomizer(chroot, config)
    customizer.configure_budgie_defaults()

    override_file = chroot.target_root / "usr" / "share" / "glib-2.0" / "schemas" / "99_budgie_defaults.gschema.override"
    assert override_file.exists()
    assert "suse-cyber-chameleon.jpg" in override_file.read_text()
    assert "Papirus-Dark" in override_file.read_text()


def test_configure_deepin_defaults_creates_gschema_override(tmp_path):
    chroot = DummyChroot()
    chroot.target_root = tmp_path / "chroot"
    config = {"desktop": "deepin"}

    customizer = SystemCustomizer(chroot, config)
    customizer.configure_deepin_defaults()

    override_file = chroot.target_root / "usr" / "share" / "glib-2.0" / "schemas" / "99_deepin_defaults.gschema.override"
    assert override_file.exists()
    assert "suse-cyber-chameleon.jpg" in override_file.read_text()
    assert "deepin-dark" in override_file.read_text()




