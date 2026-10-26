# Diagrams

Made with the [diagram-design](https://github.com/cathrynlavery/diagram-design)
skill, using an Exoscale skin (paper `#fafafa`, ink `#212121`, accent
`#da291c`, system fonts).

| File | Shows |
|---|---|
| `architecture` | The parts and the data path |
| `replication` | One sync cycle (sequence) |
| `lifecycle` | Migration states and commands |
| `cutover` | The cutover (sequence) |
| `create-template` | `create` with a receiver template (sequence) |
| `downtime` | Measured cutover downtime per guest (bar chart) |
| `disks` | Where each VM disk goes: snapshot chain, folded disk, volumes |
| `group-cutover` | A group cutover: tiers stopped front to back, converted in parallel, started back to front (timeline) |
| `rehearsal` | A rehearsal: copies seeded from the replicas, airgapped, reachable through the relay |

The `.html` files are the sources; the `.svg` files embedded in the docs are
exported from them. To change a diagram, edit `generate.py`, then:

```bash
python3 docs/diagrams/generate.py docs/diagrams
# from a checkout of the diagram-design repository:
for f in architecture replication lifecycle cutover create-template downtime disks group-cutover rehearsal; do
  python3 scripts/verify-geometry.py docs/diagrams/$f.html
  python3 skills/diagram-design/scripts/export_svg.py docs/diagrams/$f.html docs/diagrams/$f.svg --system-fonts
done
```
