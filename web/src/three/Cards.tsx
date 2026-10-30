import { useThree } from '@react-three/fiber'
import { Float } from '@react-three/drei'
import { animated, useSpring } from '@react-spring/three'
import { profit } from '../data/derive'
import type { Piece } from '../data/types'
import { ix, useStore } from '../store'
import { pin3d } from './anchors'
import { cardLayout, isNarrow } from './cardLayout'

export const STATUS_LABEL: Record<Piece['status'], string> = {
  listed: 'Listed',
  not_posted: 'Not posted',
  sold: 'Sold',
  lot: 'Lot',
  donated: 'Donated',
}

// Order the box's pieces: best unposted money first.
export function ordered(box: string) {
  return [...(ix.piecesIn.get(box) ?? [])].sort((a, b) => {
    const ua = a.status === 'not_posted' ? 0 : 1
    const ub = b.status === 'not_posted' ? 0 : 1
    return ua - ub || profit(b) - profit(a)
  })
}

export const cardDelay = (i: number) => 520 + i * 55

// 3D side of the item cards: invisible points that rise out of the open box
// and float. The cards themselves are DOM (ui/Labels.tsx) pinned to them.
export function Cards({ box }: { box: string }) {
  const place = ix.place.get(box)!
  const pieces = ordered(box)
  const narrow = isNarrow(useThree(s => s.size.width))
  const lay = cardLayout(pieces.length, place.dims[1], narrow)
  const selected = useStore(s => s.piece)
  return (
    <group position={place.openPos}>
      {pieces.map((p, i) => (
        <CardPoint
          key={p.code}
          code={p.code}
          delay={cardDelay(i)}
          to={selected === p.code ? lay.focus : lay.slots[i]}
          start={[0, place.dims[1] * 0.4, 0]}
          still={selected === p.code}
        />
      ))}
    </group>
  )
}

function CardPoint({ code, delay, to, start, still }: {
  code: string; delay: number; to: [number, number, number]; start: [number, number, number]; still: boolean
}) {
  const { x, y, z } = useSpring({
    from: { x: start[0], y: start[1], z: start[2] },
    to: { x: to[0], y: to[1], z: to[2] },
    delay: still ? 0 : delay,
    config: { tension: 150, friction: 18 },
  })
  return (
    <animated.group position-x={x} position-y={y} position-z={z}>
      <Float speed={still ? 0 : 1.6} rotationIntensity={0} floatIntensity={still ? 0 : 0.25} floatingRange={[-0.03, 0.03]}>
        <group ref={pin3d(`card:${code}`)} />
      </Float>
    </animated.group>
  )
}
