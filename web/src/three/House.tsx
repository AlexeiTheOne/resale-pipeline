import { useMemo } from 'react'
import { pin3d } from './anchors'
import { animated, useSpring } from '@react-spring/three'
import { roomCenter, search } from '../data/derive'
import type { Room } from '../data/types'
import { ix, useStore } from '../store'
import { Box3D, type BoxMode } from './Box3D'
import { Cards } from './Cards'

const WALL_H = 1.05
const WALL_T = 0.1
const WALL = '#f6f2ec'
const WALL_TOP = '#d9d1c5'

// Door gaps so it reads as a house. [room, side, from, to] in metres along the wall.
const DOORS: Record<string, [number, number]> = {
  'G:back': [3.6, 4.6],
  'R:back': [0.6, 1.6],
  'R:left': [3.2, 4.2],
  'S:left': [1.2, 2.1],
}

export function House() {
  const level = useStore(s => s.level)
  const room = useStore(s => s.room)
  const open = useStore(s => s.box)
  const hover = useStore(s => s.hoverBox)
  const peek = useStore(s => s.peek)
  const query = useStore(s => s.query)
  const setHover = useStore(s => s.setHover)
  const goTo = useStore(s => s.goTo)

  const lit = useMemo(() => new Set(search(ix, query).map(h => h.box)), [query])

  return (
    <group>
      {/* plinth */}
      <mesh position={[-0.5, -0.32, 0.5]} receiveShadow>
        <boxGeometry args={[12.4, 0.4, 11.4]} />
        <meshStandardMaterial color="#e6dfd4" roughness={1} />
      </mesh>

      {ix.inv.rooms.map(r => (
        <RoomShell key={r.code} room={r} dim={level !== 'house' && room !== r.code} />
      ))}

      {ix.inv.boxes.map(b => {
        const place = ix.place.get(b.code)!
        let mode: BoxMode = 'normal'
        if (open === b.code) mode = 'open'
        else if (level === 'box') mode = b.room === room ? 'faded' : 'dim'
        else if (level === 'room') {
          if (b.room !== room) mode = 'dim'
          else if (place.row < peek) mode = 'faded'
        }
        return (
          <Box3D
            key={b.code}
            code={b.code}
            place={place}
            stats={ix.boxStats.get(b.code)!}
            mode={mode}
            hovered={hover === b.code}
            highlighted={lit.has(b.code) && open !== b.code}
            ghost={level === 'box'}
            onHover={setHover}
            onPick={c => goTo(c)}
          />
        )
      })}

      {level === 'box' && open && <Cards box={open} />}
      <HoverTip code={level === 'box' ? null : hover} />
    </group>
  )
}

function RoomShell({ room, dim }: { room: Room; dim: boolean }) {
  const level = useStore(s => s.level)
  const goRoom = useStore(s => s.goRoom)
  const { opacity } = useSpring({ opacity: dim ? 0.18 : 1 })
  const [cx, cz] = roomCenter(room)
  const right = room.x >= 0

  const walls: { key: string; len: number; at: [number, number, number]; rot: number }[] = []
  const add = (key: string, len: number, start: [number, number], dir: 'x' | 'z') => {
    const gap = DOORS[`${room.code}:${key}`]
    const parts: [number, number][] = gap ? [[0, gap[0]], [gap[1], len]] : [[0, len]]
    parts.forEach(([a, b], i) => {
      const mid = (a + b) / 2
      walls.push({
        key: `${key}${i}`,
        len: b - a,
        at: dir === 'x' ? [start[0] + mid, WALL_H / 2, start[1]] : [start[0], WALL_H / 2, start[1] + mid],
        rot: dir === 'x' ? 0 : Math.PI / 2,
      })
    })
  }
  add('back', room.w, [room.x, room.z], 'x')
  add('left', room.d, [room.x, room.z], 'z')
  if (right) add('right', room.d, [room.x + room.w, room.z], 'z')

  return (
    <group>
      <mesh
        position={[cx, -0.06, cz]}
        receiveShadow
        onClick={e => { if (level === 'house') { e.stopPropagation(); goRoom(room.code) } }}
      >
        <boxGeometry args={[room.w - 0.02, 0.12, room.d - 0.02]} />
        <animated.meshStandardMaterial color={room.floor} roughness={room.code === 'G' ? 0.95 : 0.7} transparent opacity={opacity.to(o => 0.55 + o * 0.45)} />
      </mesh>
      {room.code !== 'G' && (
        // a rug, so bedrooms don't read as warehouse
        <mesh position={[cx, 0.003, room.z + room.d - 0.55]} rotation-x={-Math.PI / 2} receiveShadow>
          <planeGeometry args={[room.w * 0.55, 0.7]} />
          <animated.meshStandardMaterial color={room.code === 'O' ? '#b9c6c9' : room.code === 'S' ? '#e3c3b3' : '#c7c0a8'} roughness={1} transparent opacity={opacity} />
        </mesh>
      )}
      {walls.map(wl => (
        <group key={wl.key} position={wl.at} rotation-y={wl.rot}>
          <mesh castShadow={!dim} receiveShadow>
            <boxGeometry args={[wl.len + WALL_T, WALL_H, WALL_T]} />
            <animated.meshStandardMaterial color={WALL} roughness={0.9} transparent opacity={opacity} />
          </mesh>
          <mesh position={[0, WALL_H / 2 + 0.003, 0]} rotation-x={-Math.PI / 2}>
            <planeGeometry args={[wl.len + WALL_T, WALL_T]} />
            <animated.meshBasicMaterial color={WALL_TOP} transparent opacity={opacity} />
          </mesh>
        </group>
      ))}

      <group ref={pin3d(`room:${room.code}`)} position={[cx, 2.1, cz]} />
    </group>
  )
}

// Point the hover tooltip (ui/Labels.tsx) is pinned to.
function HoverTip({ code }: { code: string | null }) {
  const b = code ? ix.place.get(code) : null
  return <group ref={pin3d('tip')} position={b ? [b.pos[0], b.pos[1] + b.dims[1] + 0.25, b.pos[2]] : [0, -50, 0]} />
}
