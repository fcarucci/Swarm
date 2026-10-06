@echo off
uv run --no-project --quiet --with tree-sitter==0.26.0 --with tree-sitter-rust==0.24.2 --with radon==6.0.1 --with vulture==2.16 --with pytest python "%~dp0selftest.py" %*
