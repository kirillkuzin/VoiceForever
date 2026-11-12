# The addon

The addon is an invisible input field inside the game. The Windows app presses the addon's hotkey, pastes a line and presses Enter; the addon posts it to the right channel. The chat window never opens, the addon picks party, raid or instance chat by itself and replies to the last whisper. While you record from the gamepad it shows a recording badge in the game.

The addon has no network access: everything reaches it as keyboard input. Its messages follow the language of your WoW client: English, Russian, German, Spanish or French (English for any other).

## Install

Download [`VoiceForever-addon.zip`](https://github.com/kirillkuzin/VoiceForever/releases/latest/download/VoiceForever-addon.zip) from the latest release, or take the `addon/VoiceForever` folder from this repository, and put the `VoiceForever` folder into `Interface\AddOns` in your WoW Forever folder.

The addon is built for interface 16001. If the game calls it out of date after a patch, turn on "Load out of date AddOns", or put the number from `/dump select(4, GetBuildInfo())` into `VoiceForever.toc`.

## Commands

| Command | What it does |
|---|---|
| `/vf` | shows the agent hotkey and the current group channel |
| `/vf key CTRL-SHIFT-F12` | changes the agent hotkey (set the same key in the Windows app) |
| `/vf test` | sends a test line to your group |
| `/vf ptt` | shows the push-to-talk button and mode |
| `/vf ptt off` | gives the push-to-talk button back to the game |
| `/vf names` | shows what the addon collected for the server: channels and how many nicknames |

The default agent hotkey is `CTRL-SHIFT-F12`.

## Push-to-talk button

The addon takes the gamepad button the app records with (an override binding), so the game's own action on it is paused while the addon is on. The in-game check in the Windows app sets this up: see [Voice from the gamepad](agent.md#voice-from-the-gamepad).

## Names and channels

The addon collects what helps the server spell nicknames and find chat channels: the channels you joined (number and name), your group, guild members online or seen in the last 14 days (up to 300), your friends list (and Battle.net friends playing WoW Forever), the last 20 people you whispered with, the last 30 people who wrote in say, yell, group, guild or a custom channel, and your target. It keeps only who, where and when: the text of other players' messages is never kept.

The snapshot reaches the app two ways: in the addon's saved file, which the game writes on `/reload` and logout, and live, when you start a recording: the app asks the addon for it, the addon puts it into its input field, and the app copies it with Ctrl+C (addons cannot use the clipboard themselves). The app sends it to your server, which keeps it in memory only. You can turn both off in the app's settings.

With it you can say "в канал Рейдеры, …" (channel Raiders) and the addon finds the channel's number when it sends, so a channel that moved to another number still works. If you are not in that channel, the addon says so and sends nothing.

## Line format

The app types `VF:<channel>:<target>:<text>`, for example `VF:whisper:Arthorn:On my way`. Channels: `say`, `yell`, `emote`, `group` (party, raid or instance, whichever you are in), `party`, `raid`, `raid_warning`, `instance`, `guild`, `officer`, `whisper`, `reply` (the last person who whispered you), `channel` (target is the channel number or name). `VF:export:<nonce>:` asks for the names snapshot: the addon answers with `VFX1 <nonce> <json>` in its field, selected.
