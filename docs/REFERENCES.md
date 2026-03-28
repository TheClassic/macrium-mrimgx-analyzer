# References

Macrium Analyzer does not vendor the upstream `.mrimgx` format reference tree.

Primary upstream reference:
- `https://github.com/macriumsoftware/mrimg_file_layout`

Useful upstream documents:
- `https://github.com/macriumsoftware/mrimg_file_layout/tree/main/docs`
- `https://github.com/macriumsoftware/mrimg_file_layout/tree/main/schema`
- `https://macrium.github.io/mrimgx_file_layout/doxygen/html/files.html`

Notes:
- The analyzer's Python implementation is independent and does not build or link against the upstream C++ project.
- The persisted JSON report shape in this repo is our own analyzer output, not the upstream schema.
- If local offline experimentation is useful later, a clone of the upstream project can live under `third_party/` without being tracked by git.
