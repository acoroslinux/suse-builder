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
