---
name: rotpl-brief
description: Produce the ready-to-paste design brief for designing a new ROTPL broadcast-overlay template (rocket_test_overlay video compositor) in an external design tool such as Claude Design. Use this whenever the user wants to start designing a new overlay/template/theme for rocket motor static-fire test video, wants a prompt or brief to hand to Claude Design (or a similar tool) for a new overlay look, or asks what data a template is allowed to show before starting a design. This is the starting point of template work - once the user brings back a finished design, switch to the rotpl-build skill instead to turn it into a working file.
---

# ROTPL design brief

When the user wants to design a new overlay template, hand them `assets/design-brief.txt` as-is —
read it, then paste its exact contents into the conversation with the external design tool (Claude
Design or similar). It already states the canvas size (1920×1080), the complete real data catalog the
overlay is allowed to show, and the 9 element kinds the rendering engine actually supports (text, logo,
image, rectangles, lines, a gradient scrim, a vertical gauge, a horizontal bar gauge, a phase list — no
charts, no free-form shapes).

Don't improvise a new brief inline — the point of this file is that it's the single reusable prompt, so
the same request never needs re-explaining. Only edit `assets/design-brief.txt` itself if the user asks
for something the current brief doesn't cover (e.g. a different canvas size or a new real data field),
so the next use of this skill stays accurate.

If the user just asks "what data can a template show" without wanting a full brief, the data catalog
inside `assets/design-brief.txt` is the complete, authoritative answer on its own — don't invent fields
that aren't listed there.

## What happens after the design comes back

This skill's job stops at producing the brief. Once the user has a finished design (a screenshot, HTML,
or a description of what they want), that's a different, more technical job — switch to the
**rotpl-build** skill, which knows the exact JSON schema, has a tested build/validate/upload pipeline,
and turns a design into a real file the user can upload. Don't try to hand-author the actual template
package from this skill.
