import re

for project in ["fedora-builder", "suse-builder"]:
    with open(f"/iso-builder/{project}/cli.py", "r") as f:
        pass
        # I just want to check unmount_all_under
