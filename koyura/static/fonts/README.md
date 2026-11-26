# Fonts

Self-hosted so the dashboard needs no font CDN and the CSP can stay at
`font-src 'self'`. All three families are licensed under the SIL Open Font
License 1.1 (https://scripts.sil.org/OFL).

| File | Family | Weights | Source |
|------|--------|---------|--------|
| `inter-latin-var.woff2` | Inter | 100–900 (variable, latin subset) | Google Fonts |
| `jetbrains-mono-latin-var.woff2` | JetBrains Mono | 400–800 (variable, latin subset) | Google Fonts |
| `shippori-mincho-700.woff2` | Shippori Mincho | 700 (subset, see below) | Google Fonts |
| `shippori-mincho-800.woff2` | Shippori Mincho | 800 (subset, see below) | Google Fonts |

The Shippori Mincho files are subset with `fonttools` to the characters the UI
actually renders — digits, punctuation and the decorative glyphs
`コユラリンク送る選ぶ待つ届く動画音声保存空誤速静一二三四` — which keeps them at
~7 KB each instead of 8 MB. To extend the glyph set, re-subset the upstream
TTF from https://github.com/google/fonts/tree/main/ofl/shipporimincho with:

    pyftsubset ShipporiMincho-Bold.ttf --text-file=chars.txt \
      --flavor=woff2 --output-file=shippori-mincho-700.woff2 \
      --layout-features='' --no-hinting

If a glyph is missing, the browser falls back to `Hiragino Mincho ProN`/serif
as declared in `--font-serif`.
