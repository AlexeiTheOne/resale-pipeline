import { useMemo, useRef } from 'react'
import * as THREE from 'three'
import { useFrame } from '@react-three/fiber'
import { animated, useSpring } from '@react-spring/three'
import { CATEGORY, money, type BoxPlace, type Stats } from '../data/derive'
import { kraftTexture, labelTexture } from './textures'

export type BoxMode = 'normal' | 'faded' | 'dim' | 'open'

interface Props {
  code: string
  place: BoxPlace
  stats: Stats
  mode: BoxMode
  hovered: boolean
  highlighted: boolean
  /** Another box is open: push everything else way back. */
  ghost?: boolean
  onHover: (code: string | null) => void
  onPick: (code: string) => void
}

const T = 0.014 // cardboard thickness
const NO_RAY = () => null
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const Mat = animated.meshStandardMaterial as any // its prop types blow up tsc

export function Box3D({ code, place, stats, mode, hovered, highlighted, ghost, onHover, onPick }: Props) {
  const [w, h, d] = place.dims
  const open = mode === 'open'
  const cat = CATEGORY[stats.category]

  const at = open ? place.openPos : place.pos
  const { x, y, z, rotY, opacity } = useSpring({
    x: at[0],
    y: at[1] + (hovered && !open ? 0.03 : 0),
    z: at[2],
    rotY: open ? 0 : place.rotY,
    opacity: mode === 'faded' ? (ghost ? 0.035 : 0.1) : mode === 'dim' ? (ghost ? 0.05 : 0.3) : 1,
    config: { tension: 170, friction: 24 },
  })
  const { flap } = useSpring({
    flap: open ? 2.1 : 0,
    delay: open ? 420 : 0,
    config: { tension: 120, friction: 14 },
  })

  const tex = useMemo(() => {
    const t = kraftTexture().clone()
    t.repeat.set(w * 2, h * 2)
    t.needsUpdate = true
    return t
  }, [w, h])
  const label = useMemo(
    () => labelTexture(code, cat.color, stats.count ? `${stats.count} items · ${money(stats.value)}` : 'empty'),
    [code, cat.color, stats.count, stats.value],
  )

  const solid = mode === 'normal' || mode === 'open'
  const ray = mode === 'faded' ? NO_RAY : undefined
  const tint = useMemo(() => new THREE.Color(place.kraft), [place.kraft])
  const inside = useMemo(() => tint.clone().multiplyScalar(0.72), [tint])

  // Shared look for every cardboard panel of this box.
  const card = (inner = false) => (
    <Mat
      map={tex}
      color={inner ? inside : tint}
      roughness={0.92}
      metalness={0}
      transparent
      opacity={opacity}
      emissive={hovered && !open ? '#ffb070' : '#000000'}
      emissiveIntensity={hovered && !open ? 0.12 : 0}
    />
  )
  const tape = () => (
    <Mat color={cat.color} roughness={0.35} metalness={0.05} transparent opacity={opacity} />
  )

  const events = {
    onPointerOver: (e: { stopPropagation: () => void }) => { e.stopPropagation(); onHover(code); document.body.style.cursor = 'pointer' },
    onPointerOut: () => { onHover(null); document.body.style.cursor = '' },
    onClick: (e: { stopPropagation: () => void }) => { e.stopPropagation(); if (!open) onPick(code) },
  }

  return (
    <animated.group position-x={x} position-y={y} position-z={z} rotation-y={rotY} {...(mode === 'faded' ? {} : events)}>
      {/* walls */}
      <mesh raycast={ray} castShadow={solid} receiveShadow position={[0, T / 2, 0]}>
        <boxGeometry args={[w, T, d]} />
        {card(true)}
      </mesh>
      <mesh raycast={ray} castShadow={solid} receiveShadow position={[0, h / 2, d / 2 - T / 2]}>
        <boxGeometry args={[w, h, T]} />
        {card()}
      </mesh>
      <mesh raycast={ray} castShadow={solid} receiveShadow position={[0, h / 2, -d / 2 + T / 2]}>
        <boxGeometry args={[w, h, T]} />
        {card()}
      </mesh>
      {[-1, 1].map(s => (
        <group key={s} position={[s * (w / 2 - T / 2), 0, 0]}>
          <mesh raycast={ray} castShadow={solid} receiveShadow position={[0, h / 2, 0]}>
            <boxGeometry args={[T, h, d - 2 * T]} />
            {card()}
          </mesh>
          {/* hand hole */}
          <mesh raycast={NO_RAY} position={[s * (T / 2 + 0.001), h * 0.74, 0]} rotation-y={s * Math.PI / 2}>
            <planeGeometry args={[Math.min(0.16, d * 0.34), 0.045]} />
            <animated.meshBasicMaterial color="#3b2a1a" transparent opacity={opacity} />
          </mesh>
          {/* tape running down the side */}
          <mesh raycast={NO_RAY} position={[s * (T / 2 + 0.0015), h - 0.06, 0]} rotation-y={s * Math.PI / 2}>
            <planeGeometry args={[0.07, 0.12]} />
            {tape()}
          </mesh>
        </group>
      ))}

      {/* flaps: hinged at the front and back top edges, tape on the seam */}
      {[1, -1].map(s => (
        <animated.group key={s} position={[0, h, s * (d / 2)]} rotation-x={flap.to(f => s * f)}>
          <mesh raycast={ray} castShadow={solid} position={[0, T / 2, -s * (d / 4 - 0.002)]}>
            <boxGeometry args={[w - 0.004, T, d / 2 - 0.004]} />
            {card()}
          </mesh>
          <mesh raycast={NO_RAY} position={[0, T + 0.0012, -s * (d / 2 - 0.022)]} rotation-x={-Math.PI / 2}>
            <planeGeometry args={[w - 0.002, 0.04]} />
            {tape()}
          </mesh>
        </animated.group>
      ))}

      {/* front label */}
      <mesh raycast={NO_RAY} position={[0, h * 0.5, d / 2 + 0.0015]}>
        <planeGeometry args={[Math.min(w * 0.62, 0.42), Math.min(w * 0.62, 0.42) / 2]} />
        <Mat map={label} roughness={0.6} transparent opacity={opacity} />
      </mesh>

      {highlighted && <Glow w={w} h={h} d={d} />}

    </animated.group>
  )
}

function Glow({ w, h, d }: { w: number; h: number; d: number }) {
  const mat = useRef<THREE.MeshBasicMaterial>(null)
  useFrame(({ clock }) => {
    if (mat.current) mat.current.opacity = 0.22 + Math.sin(clock.elapsedTime * 4) * 0.14
  })
  return (
    <mesh raycast={NO_RAY} position={[0, h / 2, 0]}>
      <boxGeometry args={[w + 0.06, h + 0.06, d + 0.06]} />
      <meshBasicMaterial ref={mat} color="#ffd34d" transparent depthWrite={false} blending={THREE.AdditiveBlending} />
    </mesh>
  )
}
