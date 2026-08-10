# Broken YouTube feeds — found 2026-08-10

23 native YouTube feeds return HTTP 404: their stored `channel_id` is stale/renamed,
so these voices silently collect zero YouTube posts. Found during newsletter maintenance.
`fetch_youtube_posts` used a bare `except:` that hid this; now fixed to log (collect.py).

To fix: re-resolve each channel_id (visit youtube.com/@handle, read `externalId`),
verify `feeds/videos.xml?channel_id=<new>` returns `<entry>` items, then update voices.json.
YouTube rate-limits rapid lookups — resolve slowly (a few at a time).

| id | stored (dead) channel_id | youtube handle |
|----|----|----|
| theo-von | UC5AQEUAwCh1sGDvkQobt1dw | TheoVon |
| full-send | UCwMDFutAGCTTb-wUMVwXKDg | FULLSENDPODCAST |
| candace-owens | UCL0u5uz7KH9iy-XBbGCqemQ | CandaceOwensPodcast |
| phil-mcgraw | UCfCLrzQ3OaLJSAGfAOmfjWQ | DrPhil |
| tucker-carlson | UCEbnEBMJHQkx1rcHfJvj3Cg | TuckerCarlson |
| dan-bongino | UCVStvibG0gkwMU_WM42TSjA | BonginoReport |
| lex-fridman | UCSHZKJJfhK61IS3o76uAkDA | lexfridman |
| asmongold | UCQeRaTukNYft1_6AZPNvImw | Asmongold |
| megyn-kelly | UCEghn6GXHuh-OweS0lcbBeA | MegynKelly |
| pod-save-america | UCs7nznrHmSyNNFl4Fgd7tMQ | CrookedMedia |
| don-lemon | UCQGqTCJlmMEFC_KMYMxit5w | TheDonLemonShow |
| charlamagne-tha-god | UCBFg4JMHESwUN2V63LhCA9A | BrilliantIdiotsTV |
| chris-cuomo | UCMSvErhJfJdGfOsaLZVJLhA | ChrisCuomo |
| hasan-minhaj | UCyTXHfyaDy996UwXBYonVow | HasanMinhaj |
| glenn-greenwald | UCbnlBWwMBiECBrExsBJMB7w | GlennGreenwald |
| jason-whitlock | UCp4oTSBkr-S7r5cRVnfDaqg | JasonWhitlock |
| mark-levin | UCLoEK7HTLmQqATo5bfAi_KQ | (none stored) |
| kai-cenat | UCavTGojUqfDi9Kf4yxCM4lA | KaiCenat |
| rachel-maddow | UC_9FzicajUNLvwUsuAoqOuw | (none stored) |
| sean-hannity | UC-jWyoXTGy12xFU_f_nJLwA | (none stored) |
| jidion | UC2imo__4GBrjjNU1l-Z9TpA | JiDion |
| drew-gooden | UCTSRIY3GLFYIpkR2QAhr8BQ | DrewGooden |
| damon-imani | UCDamonImani | (none stored) |
