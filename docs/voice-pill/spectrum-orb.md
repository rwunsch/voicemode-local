# Spectrum 2 Orb Pill

The Orb starts docked at the screen edge and stays on top. The first appearance shows
the horizontal controls briefly. Then hover or explicit keyboard focus reveals
the controls, and the native window shrinks to the Orb when they fade. The compact,
strip and settings frames share the same screen anchor. More contains Voice settings,
Pause, docking, tray and keyboard actions. The classic appearance remains available
through the existing `VOICEMODE_PILL_APPEARANCE=classic` setting.

The Orb uses the approved 18 November FELIX exports: the still and the recorded
motion frames. The asset preparation crops the Orb itself to transparent
circles and rescales the frames for the 40 pt surface. It does not retint the
art or include the underlying reference screenshot, mockup chrome or demo halo.
The pill draws its own outer halo from measured audio levels, and the face is
still when reduced motion is requested. The blue listener and warm speaker
crossfade. The mute shade blends over the face and the blue listening state
does not appear while input is blocked.

To prepare the assets again, pass the approved still and PNG frame directory
to `scripts/build-pill-orb-art.py`. It requires Pillow at build time only.

Mute is stored per VoiceMode install. It does not Pause output or mute other apps.
The recorder waits without opening an input while muted, closes a recording if it
notices a mute, and discards any take that crossed a mute toggle before transcription.
The separate voice-interrupt detector also keeps its input closed while muted.
Ending voice hides the Pill for that floor until its next turn; it does not dismiss
an unrelated conversation if the active floor changed before the click arrived.

Spectrum Web Components 1.12.6 run from a checked-in local bundle so no browser
network access or Node installation is needed at runtime. To regenerate the bundle
and its notices: `cd scripts/pill-spectrum && npm ci && npm run build`.
