#!/bin/bash
echo "Starting PyInstaller Build for macOS..."

# Clean previous builds
rm -rf build dist *.spec

# Run PyInstaller
# --onefile: Create a single executable file
# --name: Name of the executable
# --add-data: Include the 'libs' directory (format: source:dest)
# --hidden-import: Ensure rich dependencies are found (sometimes needed)

pyinstaller --noconfirm --onefile --console --name "LiepinScraper" \
    --add-data "libs:libs" \
    --hidden-import "rich" \
    --hidden-import "rich.live" \
    --hidden-import "rich.progress" \
    --hidden-import "rich.console" \
    --hidden-import "rich.panel" \
    --hidden-import "rich.table" \
    --hidden-import "rich.prompt" \
    main.py

echo "Build Complete!"
echo "You can find the executable in the 'dist' folder."
echo "To test it, run: ./dist/LiepinScraper"
