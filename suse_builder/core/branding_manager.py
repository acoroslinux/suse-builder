import os
import shutil
import json
import logging
from pathlib import Path
from typing import Dict, Any

from suse_builder.core.path_utils import resolve_from_project

logger = logging.getLogger("branding")


class BrandingManager:
    """
    Applies custom visual branding to the OS, including OS name overrides,
    GRUB backgrounds, Plymouth themes, and Desktop wallpapers.
    """

    def __init__(self, chroot_manager, config: Dict[str, Any]):
        self.chroot = chroot_manager
        self.config = config
        self.branding_cfg = {}
        
        branding_file = resolve_from_project("configs/branding.json")
        if branding_file.exists():
            try:
                self.branding_cfg = json.loads(branding_file.read_text())
            except Exception as e:
                logger.error(f"Failed to load branding.json: {e}")

    def apply_branding(self):
        """Main entry point to apply all branding settings."""
        if not self.branding_cfg:
            return
            
        os_name = self.branding_cfg.get('os_name', 'Custom OS')
        logger.info(f"🎨 Applying branding profile: {os_name}")
        
        self._apply_custom_files_overlay()
        self._apply_os_release()
        self._apply_grub_branding()
        self._apply_desktop_branding()
        self._apply_plymouth_branding()
        self._apply_dconf_branding()

    def _apply_custom_files_overlay(self):
        """Intelligently copies files from configs/custom_files to their respective system locations."""
        custom_base = resolve_from_project("configs/custom_files")
        if not custom_base.exists() or not custom_base.is_dir():
            return
            
        mapping = {
            "applications": "usr/share/applications",
            "autostart": "etc/xdg/autostart",
            "backgrounds": "usr/share/backgrounds",
            "boot": "boot",
            "dconf": "etc/dconf",
            "default": "etc/default",
            "etc": "etc",
            "icons": "usr/share/icons",
            "lightdm": "etc/lightdm",
            "plymouth": "usr/share/plymouth",
            "samba": "etc/samba",
            "sddm": "etc/sddm.conf.d",
            "skel": "etc/skel",
            "sudoers.d": "etc/sudoers.d",
            "themes": "usr/share/themes",
            "usr": "usr"
        }
        
        for src_name, tgt_path in mapping.items():
            src_dir = custom_base / src_name
            if src_dir.exists():
                tgt_dir = self.chroot.target_root / tgt_path
                tgt_dir.mkdir(parents=True, exist_ok=True)
                if src_dir.is_dir():
                    shutil.copytree(src_dir, tgt_dir, dirs_exist_ok=True)
                    logger.info(f"  -> Applied overlay: {src_name}/ to /{tgt_path}/")

    def _apply_dconf_branding(self):
        """Copies custom dconf settings (GNOME) and updates the database."""
        dconf_source = resolve_from_project("configs/custom_files/dconf")
        if not dconf_source.exists() or not dconf_source.is_dir():
            return
            
        dconf_target = self.chroot.target_root / "etc" / "dconf"
        
        # Copy db/local.d and profile over to the chroot
        for sub in ["db/local.d", "profile"]:
            src = dconf_source / sub
            if src.exists():
                tgt = dconf_target / sub
                tgt.parent.mkdir(parents=True, exist_ok=True)
                # If target directory exists, copytree needs dirs_exist_ok=True (Python 3.8+)
                shutil.copytree(src, tgt, dirs_exist_ok=True) if src.is_dir() else shutil.copy2(src, tgt)
                
        # Run dconf update inside chroot
        try:
            self.chroot.run_in_chroot(["dconf", "update"], check=False)
            logger.info("  -> Injected custom dconf settings and updated database")
        except Exception as e:
            logger.warning(f"  -> Failed to update dconf: {e}")

    def _apply_plymouth_branding(self):
        """Copies Plymouth theme and sets it as default."""
        theme_dir_str = self.branding_cfg.get("plymouth_theme_dir")
        if not theme_dir_str:
            return
            
        source_dir = resolve_from_project(theme_dir_str)
        if not source_dir.exists() or not source_dir.is_dir():
            logger.warning(f"  -> Plymouth theme directory not found at {source_dir}")
            return
            
        theme_name = source_dir.name
        target_dir = self.chroot.target_root / "usr" / "share" / "plymouth" / "themes" / theme_name
        
        # Copy the theme over
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(source_dir, target_dir)
        
        # Execute plymouth-set-default-theme inside the chroot to update initramfs
        try:
            self.chroot.run_in_chroot(["plymouth-set-default-theme", "-R", theme_name], check=False)
            logger.info(f"  -> Injected custom Plymouth theme '{theme_name}' and rebuilt initramfs")
        except Exception as e:
            logger.warning(f"  -> Failed to set Plymouth theme: {e}")

    def _apply_os_release(self):
        """Overrides the PRETTY_NAME and NAME in /etc/os-release to match branding."""
        os_name = self.branding_cfg.get("os_name")
        if not os_name:
            return
            
        os_release_path = self.chroot.target_root / "usr" / "lib" / "os-release"
        etc_os_release = self.chroot.target_root / "etc" / "os-release"
        
        for path in [os_release_path, etc_os_release]:
            if path.exists() and not path.is_symlink():
                content = path.read_text()
                new_content = []
                for line in content.splitlines():
                    if line.startswith("PRETTY_NAME="):
                        new_content.append(f'PRETTY_NAME="{os_name}"')
                    elif line.startswith("NAME="):
                        new_content.append(f'NAME="{os_name}"')
                    else:
                        new_content.append(line)
                path.write_text("\n".join(new_content) + "\n")
        logger.info(f"  -> Set OS Name to '{os_name}'")

    def _apply_grub_branding(self):
        """Copies the GRUB background image and configures /etc/default/grub."""
        bg_path = self.branding_cfg.get("grub_background")
        if not bg_path:
            return
            
        source_bg = resolve_from_project(bg_path)
        if not source_bg.exists():
            logger.warning(f"  -> GRUB background not found at {source_bg}")
            return
            
        target_dir = self.chroot.target_root / "boot" / "grub2" / "themes"
        target_dir.mkdir(parents=True, exist_ok=True)
        
        target_bg = target_dir / source_bg.name
        shutil.copy2(source_bg, target_bg)
        
        # Modify /etc/default/grub
        grub_default = self.chroot.target_root / "etc" / "default" / "grub"
        if grub_default.exists():
            content = grub_default.read_text()
            lines = content.splitlines()
            has_bg = False
            for i, line in enumerate(lines):
                if line.startswith("GRUB_BACKGROUND="):
                    lines[i] = f'GRUB_BACKGROUND="/boot/grub2/themes/{source_bg.name}"'
                    has_bg = True
            
            if not has_bg:
                lines.append(f'GRUB_BACKGROUND="/boot/grub2/themes/{source_bg.name}"')
                
            grub_default.write_text("\n".join(lines) + "\n")
            logger.info("  -> Injected custom GRUB background")

    def _apply_desktop_branding(self):
        """Copies custom wallpapers to standard system locations."""
        wallpaper_path = self.branding_cfg.get("desktop_wallpaper")
        if not wallpaper_path:
            return
            
        source_wp = resolve_from_project(wallpaper_path)
        if source_wp.exists():
            wp_dir = self.chroot.target_root / "usr" / "share" / "wallpapers"
            wp_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_wp, wp_dir / source_wp.name)
            logger.info("  -> Injected custom Desktop wallpaper")
