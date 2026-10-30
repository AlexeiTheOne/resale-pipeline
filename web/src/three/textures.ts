import * as THREE from 'three'

// Cardboard: warm base with faint fibres and corrugation lines. Grey-ish so
// each box's kraft colour multiplies over it.
let kraft: THREE.CanvasTexture | null = null
export function kraftTexture() {
  if (kraft) return kraft
  const c = document.createElement('canvas')
  c.width = c.height = 256
  const g = c.getContext('2d')!
  g.fillStyle = '#ffffff'
  g.fillRect(0, 0, 256, 256)
  for (let i = 0; i < 2600; i++) {
    const v = 225 + Math.random() * 30
    g.fillStyle = `rgba(${v},${v - 6},${v - 16},${0.35 + Math.random() * 0.4})`
    g.fillRect(Math.random() * 256, Math.random() * 256, 1 + Math.random() * 6, 1)
  }
  for (let y = 0; y < 256; y += 6) {
    g.fillStyle = 'rgba(160,130,90,0.06)'
    g.fillRect(0, y, 256, 2)
  }
  kraft = new THREE.CanvasTexture(c)
  kraft.wrapS = kraft.wrapT = THREE.RepeatWrapping
  kraft.colorSpace = THREE.SRGBColorSpace
  return kraft
}

// White label sticker on the front: box code, colour stripe, item count.
const labels = new Map<string, THREE.CanvasTexture>()
export function labelTexture(code: string, color: string, line: string) {
  const key = `${code}|${color}|${line}`
  const hit = labels.get(key)
  if (hit) return hit
  const c = document.createElement('canvas')
  c.width = 512
  c.height = 256
  const g = c.getContext('2d')!
  g.fillStyle = '#fbf9f4'
  roundRect(g, 6, 6, 500, 244, 26)
  g.fill()
  g.fillStyle = color
  roundRect(g, 6, 6, 500, 50, { tl: 26, tr: 26, br: 0, bl: 0 })
  g.fill()
  g.fillStyle = '#23201c'
  g.font = '800 132px "Inter", "Segoe UI", system-ui, sans-serif'
  g.textAlign = 'center'
  g.textBaseline = 'middle'
  g.fillText(code, 256, 148)
  g.fillStyle = '#6f675c'
  g.font = '600 34px "Inter", "Segoe UI", system-ui, sans-serif'
  g.fillText(line, 256, 222)
  const t = new THREE.CanvasTexture(c)
  t.colorSpace = THREE.SRGBColorSpace
  t.anisotropy = 8
  labels.set(key, t)
  return t
}

type R = number | { tl: number; tr: number; br: number; bl: number }
function roundRect(g: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: R) {
  const q = typeof r === 'number' ? { tl: r, tr: r, br: r, bl: r } : r
  g.beginPath()
  g.moveTo(x + q.tl, y)
  g.lineTo(x + w - q.tr, y)
  g.quadraticCurveTo(x + w, y, x + w, y + q.tr)
  g.lineTo(x + w, y + h - q.br)
  g.quadraticCurveTo(x + w, y + h, x + w - q.br, y + h)
  g.lineTo(x + q.bl, y + h)
  g.quadraticCurveTo(x, y + h, x, y + h - q.bl)
  g.lineTo(x, y + q.tl)
  g.quadraticCurveTo(x, y, x + q.tl, y)
  g.closePath()
}
