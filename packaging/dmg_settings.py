# dmgbuild settings for Natter.dmg: a pixel-art window that says "drag Natter into Applications".
# Used by package.sh:  dmgbuild -s packaging/dmg_settings.py -D app=dist/Natter.app "Natter" dist/Natter.dmg
import os
app = defines.get("app", "dist/Natter.app")  # noqa: F821 (dmgbuild provides `defines`)

volume_name = "Natter"
format = "UDZO"
filesystem = "HFS+"
files = [app]
symlinks = {"Applications": "/Applications"}
icon_locations = {os.path.basename(app): (165, 225), "Applications": (495, 225)}  # centres, matching the background art
background = defines.get("background", os.path.join(os.getcwd(), "assets", "dmg-background.tiff"))  # noqa: F821  660x420 + @2x layer
window_rect = ((200, 120), (660, 420))
default_view = "icon-view"
show_status_bar = show_tab_view = show_toolbar = show_pathbar = show_sidebar = False
icon_size = 128
text_size = 13
