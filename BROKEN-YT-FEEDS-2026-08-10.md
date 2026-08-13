# Broken YouTube feeds — found 2026-08-10 (updated 2026-08-10)

Original scan found 23 native YouTube feeds returning 404 (stale/renamed `channel_id`),
so those voices silently collected zero YouTube posts.
`fetch_youtube_posts` now logs these failures instead of swallowing them (collect.py).

## FIXED (16) — re-resolved via yt-dlp, each verified to return a live feed

| id | new channel_id | handle |
|----|----|----|
| theo-von | UC5AQEUAwCh1sGDvkQtkDWUQ | @TheoVon |
| full-send | UChPuCAEXg7iYkVNjQY1NGYg | @FULLSENDPODCAST |
| candace-owens | UCkY4fdKOFk3Kiq7g5LLKYLw | @CandaceOwensPodcast |
| phil-mcgraw | UCYLR1ghzYNyvfjw78raCuxA | @DrPhil |
| tucker-carlson | UCGttrUON87gWfU6dMWm1fcA | @TuckerCarlson |
| lex-fridman | UCSHZKyawb77ixDdsGog4iWA | @lexfridman |
| asmongold | UCq2jigrIGtupbTXiNjq6Wrw | @zackrawrr |
| megyn-kelly | UCzJXNzqz6VMHSNInQt_7q6w | @MegynKelly |
| don-lemon | UCXs0PlIGUDSXfBaF7j-1euA | @TheDonLemonShow |
| chris-cuomo | UCGB-czkAt6nLd3UrQ1eD0uw | @ChrisCuomo |
| hasan-minhaj | UCarEovlrD9QY-fy-Z6apIDQ | @HasanMinhaj |
| glenn-greenwald | UChzVhAwzGR7hV-4O8ZmBLHg | @GlennGreenwald |
| mark-levin | UCm0ZCU4Svnq9f46AOiYvONw | @MarkLevinShow |
| kai-cenat | UCoEmptob-eEGKk18c2VplJg | @KaiCenat |
| jidion | UCvj3hNvwrEgTRkeut7_cBAQ | @JiDion |
| drew-gooden | UCjtkaY_1JrDw6uUtwvcPsTg | @DrewGooden |

## RESOLVED by dropping the dead youtube feed

| id | resolution |
|----|----|
| dan-bongino | 2026-08-12: youtube channel `UCVStvibG0gkwMU_WM42TSjA` confirmed 404 with no clean replacement. Dropped the youtube feed/platform/handle; keeps live podcast (megaphone, 200). Note: his `x` rss.app feed is also 404 and may need regenerating. |
| damon-imani | 2026-08-13: placeholder ID `UCDamonImani` (invalid, 0 posts) → repointed to his live channel `UCdUq1SGXWgdbHYVU4VQlHPA` ("Damon Imani Clips", @DamonImani, 234K subs, actively posts his satire). Verified feed 200. His main reach is X/TikTok/IG (1.6M); YouTube clips are his primary output there. |

## STILL OPEN (5) — need a manual decision (no clean canonical channel)

| id | note |
|----|----|
| pod-save-america | Content lives on the Crooked Media channel; needs a decision on which feed to track. |
| charlamagne-tha-god | Content lives on The Breakfast Club / network channels; no clean solo channel. |
| jason-whitlock | "Fearless" show is on Blaze Media; @FearlessTV resolved to an unrelated 2015 channel — needs the real Blaze channel_id. |
| rachel-maddow | MSNBC hosts her content; no standalone Maddow channel with a working feed. |
| sean-hannity | Handle @SeanHannity did not resolve; needs the current Fox/Hannity channel_id. |

These edge cases are people who don't run a standalone YouTube channel
(their content lives on a network's channel), or left the platform. Decide per voice
whether to point at a network channel, drop the youtube feed, or keep only their other platforms.
