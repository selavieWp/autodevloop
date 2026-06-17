# release.ps1 - build and publish AutoDevLoop to PyPI (Windows / PowerShell 7+).
#
# SECURITY: This script reads your PyPI API token from the environment variable
# $env:PYPI_TOKEN. NEVER hard-code a token in this file and NEVER commit a token
# to git. The token is a password to your PyPI account. If one leaks, revoke it
# at https://pypi.org/manage/account/token/ immediately.
#
# Usage:
#   $env:PYPI_TOKEN = "pypi-AgE...your-token..."   # paste once per terminal session
#   ./release.ps1                                   # upload to real PyPI
#   ./release.ps1 -Test                             # upload to TestPyPI instead
#
# Before running: bump the version in pyproject.toml AND autodevloop/__init__.py.

param(
    [switch]$Test
)

$ErrorActionPreference = "Stop"

# 1. Make sure build tools are present.
python -m pip install --upgrade build twine

# 2. Clean old artifacts so we never upload a stale build.
Remove-Item -Recurse -Force dist, build -ErrorAction SilentlyContinue
Get-ChildItem -Directory -Filter *.egg-info | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 3. Build the wheel + sdist into dist/.
python -m build

# 4. Validate the metadata renders correctly on PyPI.
python -m twine check dist/*

# 5. Upload. Token comes from the environment, not from disk.
if (-not $env:PYPI_TOKEN) {
    throw "PYPI_TOKEN is not set. Run: `$env:PYPI_TOKEN = 'pypi-...'  (do not commit it)"
}
$env:TWINE_USERNAME = "__token__"
$env:TWINE_PASSWORD = $env:PYPI_TOKEN

if ($Test) {
    python -m twine upload --repository-url https://test.pypi.org/legacy/ dist/*
    Write-Host "Uploaded to TestPyPI. Verify with:" -ForegroundColor Green
    Write-Host "  pip install -i https://test.pypi.org/simple/ autodevloop"
} else {
    python -m twine upload dist/*
    Write-Host "Uploaded to PyPI. Friends can now run:  pip install autodevloop" -ForegroundColor Green
}
