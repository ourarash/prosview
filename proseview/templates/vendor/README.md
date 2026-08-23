# Vendored front-end dependencies

These files are checked in so the dashboard works offline and so
upstream major releases can't break the page silently. They're served
by the proseview server at `/vendor/<file>`.

| File                          | Source                                    | Pinned version |
|-------------------------------|-------------------------------------------|----------------|
| chart.js                      | npm:chart.js                              | 4.5.0          |
| chartjs-plugin-annotation.js  | npm:chartjs-plugin-annotation             | 3.0.1          |
| marked.js                     | npm:marked                                | 14.1.4         |

The ProseMirror modules are pulled from `esm.sh` at fixed versions in
`templates/index.html.j2`. Vendoring the full ESM graph would require a
build step, so they're pinned in-line instead.

To upgrade a vendored file, replace it with the new minified bundle
from `https://cdn.jsdelivr.net/npm/<package>@<version>/...` and update
the version row above.
