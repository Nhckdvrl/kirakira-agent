# LXGW WenKai GB Screen runtime shards

These four WOFF2 subsets come from
`frontend/theme/assets/fonts/LXGWWenKaiGBScreen.woff2` v1.522. Together they
cover the source font's complete Unicode cmap. Unicode ranges let the browser
fetch only the subsets needed by a page.

The checked-in subsets and CSS are runtime assets. The original source font
and its SIL Open Font License remain in `frontend/theme/assets/fonts/`.
The former one-off splitting script is not part of this distribution;
`npm run build` consumes these assets directly and does not regenerate them.
