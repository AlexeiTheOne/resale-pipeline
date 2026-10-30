// Where the item cards float when a box is open. Shared by the cards and the
// camera so the camera frames all of them.
export const isNarrow = (w: number) => w <= 760

export function cardLayout(n: number, top: number, narrow: boolean) {
  const perRow = narrow ? 3 : 5
  const sx = narrow ? 0.44 : 0.5
  const rowH = 0.62
  const slots: [number, number, number][] = []
  for (let i = 0; i < n; i++) {
    const r = Math.floor(i / perRow)
    const inRow = Math.min(perRow, n - r * perRow)
    const x = (i % perRow - (inRow - 1) / 2) * sx
    slots.push([x, top + 0.52 + r * rowH - Math.abs(x) * 0.12, -0.15 - r * 0.3 - Math.abs(x) * 0.12])
  }
  const rows = Math.max(1, Math.ceil(n / perRow))
  return {
    slots,
    /** forward spot for the clicked card */
    focus: [0, top + 0.75, 0.6] as [number, number, number],
    height: top + 0.52 + (rows - 1) * rowH + 0.32,
    halfWidth: ((Math.min(perRow, n) - 1) * sx) / 2 + 0.45,
  }
}
