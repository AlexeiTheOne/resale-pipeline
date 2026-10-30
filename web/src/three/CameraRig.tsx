import { useEffect, useRef } from 'react'
import { useThree } from '@react-three/fiber'
import { CameraControls } from '@react-three/drei'
import CameraControlsImpl from 'camera-controls'
import * as THREE from 'three'
import { roomCenter } from '../data/derive'
import { ix, useStore } from '../store'

const { ACTION } = CameraControlsImpl

import { cardLayout, isNarrow } from './cardLayout'

interface Shot {
  target: [number, number, number]
  dir: [number, number, number] // from target toward camera
  halfWidth: number // what has to fit across the screen
  halfHeight?: number
  azimuth: [number, number]
  polar: [number, number]
}

function shotFor(level: string, room: string | null, box: string | null, narrow: boolean): Shot {
  if (level === 'box' && box) {
    const pl = ix.place.get(box)!
    const o = pl.openPos
    const lay = cardLayout(ix.piecesIn.get(box)?.length ?? 0, pl.dims[1], narrow)
    return {
      target: [o[0], lay.height / 2, o[2] - 0.2], dir: [0, 0.45, 1],
      halfWidth: lay.halfWidth, halfHeight: lay.height / 2 + (narrow ? 0.45 : 0.2),
      azimuth: [-0.7, 0.7], polar: [0.6, 1.4],
    }
  }
  if (level === 'room' && room) {
    const r = ix.room.get(room as never)!
    const [cx] = roomCenter(r)
    return { target: [cx, 0.55, r.z + r.d * 0.45], dir: [0, 0.62, 1], halfWidth: narrow ? r.w / 2 - 0.4 : r.w / 2 + 0.3, azimuth: [-0.55, 0.55], polar: [0.75, 1.4] }
  }
  return { target: [-0.5, 0, 0.6], dir: [0.32, 0.95, 1], halfWidth: 7.3, azimuth: [-0.5, 0.95], polar: [0.35, 1.1] }
}

export function CameraRig() {
  const ref = useRef<CameraControlsImpl>(null)
  const level = useStore(s => s.level)
  const room = useStore(s => s.room)
  const box = useStore(s => s.box)
  const { camera, size } = useThree()
  const token = useRef(0)

  useEffect(() => {
    const c = ref.current
    if (!c) return
    const shot = shotFor(level, room, box, isNarrow(size.width))
    const cam = camera as THREE.PerspectiveCamera
    // Leave room for the side panel on desktop.
    const panel = size.width > 760 ? 354 : 0
    const aspect = (size.width - panel) / size.height
    const vfov = THREE.MathUtils.degToRad(cam.fov)
    const hfov = 2 * Math.atan(Math.tan(vfov / 2) * aspect)
    const dist = Math.max(shot.halfWidth / Math.tan(hfov / 2), (shot.halfHeight ?? shot.halfWidth * 0.9) / Math.tan(vfov / 2)) * 1.05
    const dir = new THREE.Vector3(...shot.dir).normalize()
    const [tx, ty, tz] = shot.target
    const p = dir.multiplyScalar(dist).add(new THREE.Vector3(tx, ty, tz))

    // Loose limits while flying, then tighten so you can only look around a bit.
    const my = ++token.current
    c.minAzimuthAngle = -Infinity
    c.maxAzimuthAngle = Infinity
    c.minPolarAngle = 0
    c.maxPolarAngle = Math.PI
    c.minDistance = 0.5
    c.maxDistance = 100
    const perPx = (2 * dist * Math.tan(vfov / 2)) / size.height
    c.setFocalOffset(panel / 2 * perPx, 0, 0, true)
    c.setLookAt(p.x, p.y, p.z, tx, ty, tz, true).then(() => {
      if (my !== token.current) return
      c.minAzimuthAngle = shot.azimuth[0]
      c.maxAzimuthAngle = shot.azimuth[1]
      c.minPolarAngle = shot.polar[0]
      c.maxPolarAngle = shot.polar[1]
      c.minDistance = dist * 0.45
      c.maxDistance = dist * 1.6
    })
  }, [level, room, box, camera, size.width, size.height])

  return (
    <CameraControls
      ref={ref}
      makeDefault
      smoothTime={0.6}
      draggingSmoothTime={0.12}
      mouseButtons={{ left: ACTION.ROTATE, middle: ACTION.DOLLY, right: ACTION.NONE, wheel: ACTION.DOLLY }}
      touches={{ one: ACTION.TOUCH_ROTATE, two: ACTION.TOUCH_DOLLY, three: ACTION.NONE }}
    />
  )
}
