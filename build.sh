#!/bin/bash
# Build script for Speco DRV Extractor macOS app
# Usage: bash build.sh

set -e

echo "================================================"
echo "Speco DRV Extractor - macOS App Builder"
echo "================================================"
echo

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Install from python.org or via Homebrew:"
    echo "   brew install python3"
    exit 1
fi

PYTHON=$(which python3)
echo "✓ Python: $PYTHON"
echo "  Version: $($PYTHON --version)"
echo

# Create virtual environment if it doesn't exist
VENV="./venv"
if [ ! -d "$VENV" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv "$VENV"
    echo "✓ Virtual environment created"
else
    echo "✓ Virtual environment exists"
fi

# Activate virtual environment
source "$VENV/bin/activate"
PYTHON="$VENV/bin/python3"
echo "✓ Virtual environment activated"
echo "  Python: $PYTHON"
echo

# Install/upgrade PyInstaller
echo "Installing PyInstaller..."
$PYTHON -m pip install -q --upgrade pip
$PYTHON -m pip install -q PyInstaller
echo "✓ PyInstaller ready"
echo

# Check for required files
echo "Checking for required files..."
if [ ! -f "drv_gui.py" ]; then
    echo "❌ drv_gui.py not found"
    exit 1
fi
if [ ! -f "drv_extract_v11.py" ]; then
    echo "❌ drv_extract_v11.py not found"
    exit 1
fi
if [ ! -f "drv_extractor.spec" ]; then
    echo "❌ drv_extractor.spec not found"
    exit 1
fi
echo "✓ All required files present"
echo

# Clean build artifacts
echo "Cleaning old builds..."
rm -rf build dist "Speco DRV Extractor.app"
echo "✓ Cleaned"
echo

# Build the app
echo "Building macOS app bundle..."
$PYTHON -m PyInstaller drv_extractor.spec
echo

# Check result
if [ -d "dist/Speco DRV Extractor.app" ]; then
    echo "================================================"
    echo "✓ Build successful!"
    echo "================================================"
    echo
    echo "Your app is ready:"
    echo "  dist/Speco DRV Extractor.app"
    echo
    echo "To run it:"
    echo "  1. Double-click the app in Finder, or"
    echo "  2. open 'dist/Speco DRV Extractor.app'"
    echo
    echo "To install in Applications folder:"
    echo "  cp -r 'dist/Speco DRV Extractor.app' /Applications/"
    echo
else
    echo "❌ Build failed"
    exit 1
fi
