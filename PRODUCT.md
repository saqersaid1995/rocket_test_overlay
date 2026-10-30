# Product

<!-- impeccable:product-schema 1 -->

## Platform

web (Flask app opened in a browser window on a Windows PC; local only, works offline)

## Users

Engineers and operators at Stellar Kinetics (Muscat, Oman) running static-fire tests of solid rocket motors on a test stand. They watch chamber pressure, live camera, and the ignition state, then arm, count down and fire. The screen is used in every setting: a bright workshop or hangar, a dim control room, and outdoors in daylight on a laptop. Main screen is a 35-inch wide monitor; laptops too.

## Product Purpose

SFT STELLAR is the test-stand console: live chamber pressure from an ESP32 + pressure transducer, Hikvision camera preview and recording, ignition (ARM → hold-to-start countdown → FIRE pulse through an ESP32 relay, ABORT at any time), session recording (pressure CSV, events, video), ESP32 hardware scan and firmware upload over Ethernet, and a system check. Success: an operator can run a test safely and calmly, read state from a distance, and walk away with a saved, understandable result.

## Positioning

A dedicated console for one company's motor test stand, built around its own hardware and safety sequence, not a generic IoT dashboard.

## Brand Commitments

- Product name "SFT STELLAR", company "Stellar Kinetics"; existing logo (assets/logo, teststand/web/static/logo.svg).
- Owner's reference for the level of polish: SpaceX launch / mission-control webcast screens.
- Owner's verdict on the previous UI: identical boxes everywhere, weak colours, no type hierarchy, "does not look like real software".
- Interface language is English; the owner reads English as a second language, so labels stay short and plain.

## Constraints

- Safety behaviour must not change: ABORT always reachable (Esc), hold-to-start, auto-disarm, relay test only when SAFE.
- Must work fully offline: fonts and assets are bundled, no CDN.
- Do not add features or restrictions the owner did not ask for.
