import { useEffect } from 'react'
import { Scene } from './three/Scene'
import { Overlay } from './ui/Overlay'
import { Labels } from './ui/Labels'
import { ix, useStore } from './store'
import type { RoomCode } from './data/types'

// Deep links, e.g. from a Telegram sold alert: #G3-07 opens that box and
// shows the piece; #G3 opens the box; #G opens the garage.
function follow() {
  const code = decodeURIComponent(location.hash.slice(1)).toUpperCase()
  if (!code) return
  const s = useStore.getState()
  const p = ix.piece.get(code)
  if (p) return s.goTo(p.box, p.code)
  if (ix.box.has(code)) return s.goTo(code)
  if (ix.room.has(code as RoomCode)) return s.goRoom(code as RoomCode)
}

export function App() {
  useEffect(() => {
    const t = setTimeout(follow, 600)
    window.addEventListener('hashchange', follow)
    return () => { clearTimeout(t); window.removeEventListener('hashchange', follow) }
  }, [])
  return (
    <div className="app">
      <Scene />
      <Labels />
      <Overlay />
    </div>
  )
}
