# The addon

The addon is an invisible input field inside the game. The Windows app presses the addon's hotkey, pastes a line and presses Enter; the addon posts it to the right channel. The chat window never opens, the addon picks party, raid or instance chat by itself and replies to the last whisper. While you record from the gamepad it shows a recording badge in the game.

The addon has no network access: everything reaches it as keyboard input.

## Install

Download `VoiceForever-addon-<version>.zip` from [Releases](https://github.com/kirillkuzin/VoiceForever/releases), or take the `addon/VoiceForever` folder from this repository, and put the `VoiceForever` folder into `Interface\AddOns` in your WoW Forever folder.

The addon is built for interface 16001. If the game calls it out of date after a patch, turn on "Load out of date AddOns", or put the number from `/dump select(4, GetBuildInfo())` into `VoiceForever.toc`.

## Commands

| Command | What it does |
|---|---|
| `/vf` | shows the agent hotkey and the current group channel |
| `/vf key CTRL-SHIFT-F12` | changes the agent hotkey (set the same key in the Windows app) |
| `/vf test` | sends a test line to your group |
| `/vf ptt` | shows the push-to-talk button and mode |
| `/vf ptt off` | gives the push-to-talk button back to the game |

The default agent hotkey is `CTRL-SHIFT-F12`.

## Push-to-talk button

The addon takes the gamepad button the app records with (an override binding), so the game's own action on it is paused while the addon is on. The in-game check in the Windows app sets this up: see [Voice from the gamepad](agent.md#voice-from-the-gamepad).

## Line format

The app types `VF:<channel>:<target>:<text>`, for example `VF:whisper:Arthorn:On my way`. Channels: `say`, `yell`, `emote`, `group` (party, raid or instance, whichever you are in), `party`, `raid`, `raid_warning`, `instance`, `guild`, `officer`, `whisper`, `reply` (the last person who whispered you), `channel` (target is the channel number).
