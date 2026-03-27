# Spatial Guide — Etherflux World Building

## Scale Reference
| Object | Height (units) |
|---|---|
| Human | 1.8 |
| Tree | 4–10 |
| Boulder / large rock | 1–3 |
| Cabin | 4w × 4d × 3h |
| Crystal | 0.5–2 |
| Bush / grass patch | 0.3–0.8 |
| Fire (campfire) | 0.5–1.0 |
| Ruins wall segment | 1.5–4 |

Scale in scene_delta is a multiplier: scale [1,1,1] = default size. scale [2,1,2] = double wide/deep, same height.

## Spacing Rules
- **Clusters** (rocks, crystals, trees): 2–5 units apart
- **Landmarks** (cabin, large ruins, portal): 10+ units from other landmarks
- **Never** place two objects at the same [x, y, z] — they will clip
- Minimum safe distance between any two objects: 1 unit

## Composition
- Group 3–5 related objects for a scene — lone objects feel empty
- **Vary heights**: mix y=0 ground objects with y=0.5 details and y=1+ focal points
- Use **triangular placement**, not grid lines: offset objects so no three share a line
- Give scenes a focal point: one dominant object, 2–4 supporting objects

## Height Layering
| Layer | Y range | Examples |
|---|---|---|
| Ground | y = 0 | grass, path, flat rock |
| Low detail | y = 0–0.5 | small crystals, embers, pebbles |
| Eye level | y = 1–2 | standing crystals, fire, ruins base |
| Canopy / overhead | y = 4–8 | treetops, tall ruins, light sources |
| Sky features | y = 15+ | floating crystals, overhead particles |

## Common Scene Recipes

### Campfire
- `fire` at center, y=0.5, scale [0.8, 0.8, 0.8]
- 3–4 `rock` in a ring at radius 1.5, y=0, scale [0.4–0.7, 0.4, 0.4–0.7]
- 1 `light` above at y=2, intensity 1.0, warm color (#ff8844)

### Crystal Garden
- 1 large `crystal` focal point, y=0, scale [1.5, 2, 1.5]
- 3–4 smaller `crystal` around it at 2–4 unit radius, varying y=0–0.5
- `particle` emitter at y=1, low opacity, matching emissive color
- `grass` patches between crystals, scale [2,1,2], y=0

### Ruins
- 3–5 `wall` segments at varied angles (rotation y = 0, 45, 90…), y=0
- Scale walls to [1, 2–4, 0.5] — thin, tall, like standing slabs
- `rock` rubble scattered around, scale [0.3–0.8], y=0
- 1 `crystal` accent inside the ruin, emissive glow

### Forest Clearing
- 3–5 `tree` in a rough circle, radius 8–15 from center
- `grass` patches filling the floor
- `light` (directional, soft) at y=8 for ambiance
- Optional `path` cutting through

## Positioning Tips
- Current position is your anchor: place objects within radius 5–20 of where you stand
- Think in layers: floor first, then mid-height details, then vertical accents
- Use the zone's color palette: Void Wastes = grey/black, Ember Reach = orange/red, Hollow Core = teal/purple
