# geosite-cn

Small `geosite.dat` files for routers, rebuilt every day by GitHub Actions.
Each file holds exactly one tag, `CN`.

| File | Content |
|---|---|
| `geosite-cn.dat` | the `CN` tag of [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community) `dlc.dat`, copied byte-for-byte |
| `geosite-cn-enhanced.dat` | v2fly `CN` plus the [Loyalsoldier/v2ray-rules-dat](https://github.com/Loyalsoldier/v2ray-rules-dat) lists `direct-list`, `china-list`, `apple-cn` and `google-cn`, deduplicated into one tag `CN` |

Every `.dat` has a `.sha256sum` file in standard `sha256sum` format
(`<hash>  <file>`). `manifest.json` records the build time, the v2fly release
tag and `dlc.dat` sha256, the Loyalsoldier release commit and the sha256 of
each list, the rule counts by type, and the output sha256s.

## Download URLs

```
https://raw.githubusercontent.com/thrandex/geosite-cn/release/geosite-cn.dat
https://raw.githubusercontent.com/thrandex/geosite-cn/release/geosite-cn.dat.sha256sum
https://raw.githubusercontent.com/thrandex/geosite-cn/release/geosite-cn-enhanced.dat
https://raw.githubusercontent.com/thrandex/geosite-cn/release/geosite-cn-enhanced.dat.sha256sum
https://raw.githubusercontent.com/thrandex/geosite-cn/release/manifest.json
```

These URLs serve the files directly, with no redirect.

## Use with honk

honk downloads `assets.geodata.geosite` and checks it against
`<url>.sha256sum`. Point it at one of the files:

```
assets {
    geodata {
        geosite: 'https://raw.githubusercontent.com/thrandex/geosite-cn/release/geosite-cn.dat'
    }
}
```

Both files use the tag `CN`, so a rule such as `domain(geosite: cn) -> direct`
works with either one. To use the enhanced list, change the URL to
`geosite-cn-enhanced.dat`.

The enhanced file is about 15 times larger. It also sends some global hosts
direct, for example `domain:akamai.com`, `domain:steam.com` and
`full:dl.google.com`, because the Loyalsoldier lists contain them. The whole-TLD
entry `domain:top` from `china-list` is removed (`--drop-domain top`); other
TLD entries such as `domain:cn` and `domain:wang` stay.

## Update schedule

The workflow runs every day at 23:00 UTC (07:00 Beijing time), after
Loyalsoldier's daily build at 06:00 Beijing time. It can also be started by
hand (`workflow_dispatch`).

The `release` branch is replaced by one new orphan commit on every publish, so
it always has a single commit. The push is skipped when both `.dat` files are
unchanged.

## Quality gates

The job fails, and nothing is published, if any of these checks fails:

- `dlc.dat` matches the `dlc.dat.sha256sum` of its v2fly release.
- Each output holds exactly one tag, `CN`.
- `geosite-cn.dat` has at least 5,000 rules, `geosite-cn-enhanced.dat` at
  least 80,000.
- These domains are covered: `taobao.com`, `douyin.com`, `alicdn.com`,
  `bilibili.com`, `qq.com`, `baidu.com` (the enhanced build checks more).
- These domains are not covered: `tiktok.com`, `tiktokv.com`, `capcut.com`,
  `github.com`, `google.com`, `youtube.com`, `openai.com`, and `example.top`
  in the enhanced file.
- `build_geosite.py` parses its own output again before writing it and checks
  the tags and rule counts.

## Build locally

Needs `bash`, `curl` and Python 3 (standard library only).

```sh
scripts/build.sh /tmp/geosite-out
```

To rebuild from inputs you already have (no downloads):

```sh
INPUT_DIR=/path/to/inputs DLC_TAG=<v2fly release tag> LS_COMMIT=<Loyalsoldier commit> \
  scripts/build.sh /tmp/geosite-out
```

`INPUT_DIR` must hold `dlc.dat`, `dlc.dat.sha256sum`, `direct-list.txt`,
`china-list.txt`, `apple-cn.txt` and `google-cn.txt`. The same inputs give
byte-identical `.dat` files.

| Path | Purpose |
|---|---|
| `scripts/build.sh` | download, verify, build, gates; the workflow runs this |
| `scripts/build_geosite.py` | reads `dlc.dat`, copies or merges tags, spot checks |
| `scripts/manifest.py` | one-tag and size gates, writes `manifest.json` |
| `.github/workflows/build.yml` | daily build and publish to `release` |

## Attribution and license

- [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community),
  MIT License: the `CN` tag of `dlc.dat`.
- [Loyalsoldier/v2ray-rules-dat](https://github.com/Loyalsoldier/v2ray-rules-dat),
  GPL-3.0: `direct-list.txt`, `china-list.txt`, `apple-cn.txt`,
  `google-cn.txt`.

`geosite-cn-enhanced.dat` is derived from GPL-3.0 data, so this repository,
its scripts and its outputs are released under the
[GNU General Public License v3.0](LICENSE).
