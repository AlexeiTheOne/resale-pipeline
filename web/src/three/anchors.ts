// Pins normal DOM elements (room tags, tooltips, item cards) to points in the
// 3D scene. The DOM lives in the regular React tree, so it layers under the
// side panel and mounts/unmounts cleanly; one useFrame moves them all.
import { useFrame, useThree } from '@react-three/fiber'
import * as THREE from 'three'

interface Anchor {
  obj?: THREE.Object3D
  el?: HTMLElement
  /** Like drei Html's distanceFactor: grows/shrinks with distance. */
  size?: number
}
const anchors = new Map<string, Anchor>()
const get = (id: string) => {
  let a = anchors.get(id)
  if (!a) anchors.set(id, (a = {}))
  return a
}
const tidy = (id: string) => {
  const a = anchors.get(id)
  if (a && !a.obj && !a.el) anchors.delete(id)
}

/** ref for the 3D side: <group ref={pin3d('room:G')} /> */
export const pin3d = (id: string) => (o: THREE.Object3D | null) => {
  get(id).obj = o ?? undefined
  tidy(id)
}
/** ref for the DOM side: <div ref={pinEl('room:G', 2.4)} /> */
export const pinEl = (id: string, size?: number) => (el: HTMLElement | null) => {
  const a = get(id)
  a.el = el ?? undefined
  a.size = size
  tidy(id)
}

const v = new THREE.Vector3()
export function Projector() {
  const { camera, size } = useThree()
  useFrame(() => {
    const cam = camera as THREE.PerspectiveCamera
    const k = 1 / (2 * Math.tan(THREE.MathUtils.degToRad(cam.fov) / 2))
    for (const a of anchors.values()) {
      if (!a.el) continue
      if (!a.obj) { a.el.style.visibility = 'hidden'; continue }
      a.obj.getWorldPosition(v)
      const dist = v.distanceTo(cam.position)
      v.project(cam)
      if (v.z > 1) { a.el.style.visibility = 'hidden'; continue }
      const x = (v.x * 0.5 + 0.5) * size.width
      const y = (-v.y * 0.5 + 0.5) * size.height
      const s = a.size ? (a.size * k) / dist : 1
      a.el.style.transform = `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0) translate(-50%, -50%) scale(${s.toFixed(3)})`
      a.el.style.zIndex = String(Math.round((1 - v.z) * 1e6))
      a.el.style.visibility = 'visible'
    }
  })
  return null
}
