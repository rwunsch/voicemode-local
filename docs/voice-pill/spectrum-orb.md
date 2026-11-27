# Spectrum 2 Orb Pill

The Orb starts docked at the screen edge and stays on top. The first appearance shows
the horizontal controls briefly. Then hover or explicit keyboard focus reveals
the controls, and the native window shrinks to the Orb when they fade. The compact,
strip and settings frames share the same screen anchor. More contains Voice settings,
Pause, docking, tray and keyboard actions. The classic appearance remains available
through the existing `VOICEMODE_PILL_APPEARANCE=classic` setting.

The 18 November FELIX reference has a blue-violet upper cloud, brighter magenta
sides, a dark centre, a broad orange base and an independently moving star layer.
The initial Orb used a reduced CSS approximation. The revised layers bring back
more of that colour and depth without distributing the reference bitmap. The blue
listener and warm speaker crossfade. The in-Orb mute shading has no hard dividing
line; it leaves the warm speaker visible and prevents the blue listening state
while input is blocked. Animation remains scoped to the actual runtime phase
and the audio bubble uses the existing measured levels.

Mute is stored per VoiceMode install. It does not Pause output or mute other apps.
The recorder waits without opening an input while muted, closes a recording if it
notices a mute, and discards any take that crossed a mute toggle before transcription.
The separate voice-interrupt detector also keeps its input closed while muted.
Ending voice hides the Pill for that floor until its next turn; it does not dismiss
an unrelated conversation if the active floor changed before the click arrived.

Spectrum Web Components 1.12.6 run from a checked-in local bundle so no browser
network access or Node installation is needed at runtime. To regenerate the bundle
and its notices: `cd scripts/pill-spectrum && npm ci && npm run build`.
