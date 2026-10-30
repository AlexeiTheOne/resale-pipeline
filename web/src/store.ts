import { create } from 'zustand'
import { FAKE } from './data/fake'
import { buildIndex } from './data/derive'
import type { RoomCode } from './data/types'

export const ix = buildIndex(FAKE)

export type Level = 'house' | 'room' | 'box'

interface State {
  level: Level
  room: RoomCode | null
  box: string | null
  piece: string | null
  hoverBox: string | null
  /** How many front rows are see-through in room view. */
  peek: number
  query: string
  queued: string[]
  goHouse: () => void
  goRoom: (r: RoomCode) => void
  openBox: (code: string, piece?: string | null) => void
  goTo: (box: string, piece?: string | null) => void
  selectPiece: (code: string | null) => void
  back: () => void
  setHover: (code: string | null) => void
  setPeek: (n: number) => void
  setQuery: (q: string) => void
  toggleQueued: (code: string) => void
}

let flight: number | undefined
const cancel = () => window.clearTimeout(flight)

export const useStore = create<State>((set, get) => ({
  level: 'house',
  room: null,
  box: null,
  piece: null,
  hoverBox: null,
  peek: 0,
  query: '',
  queued: [],

  goHouse: () => { cancel(); set({ level: 'house', room: null, box: null, piece: null, peek: 0 }) },
  goRoom: r => { cancel(); set({ level: 'room', room: r, box: null, piece: null, peek: 0 }) },
  openBox: (code, piece = null) => {
    cancel()
    const b = ix.box.get(code)
    if (!b) return
    set({ level: 'box', room: b.room, box: code, piece })
  },
  // Fly house -> room -> box, so you see where the box lives.
  goTo: (code, piece = null) => {
    cancel()
    const b = ix.box.get(code)
    if (!b) return
    const s = get()
    if (s.level === 'box' && s.box === code) { set({ piece }); return }
    const sameRoom = s.room === b.room && s.level === 'room'
    // Fade the rows in front of a back-row box so it's visible on the way in.
    set({ level: 'room', room: b.room, box: null, piece: null, peek: b.pile.z })
    flight = window.setTimeout(() => get().openBox(code, piece), sameRoom ? 250 : 1100)
  },
  selectPiece: code => set({ piece: code }),
  back: () => {
    const s = get()
    if (s.level === 'box' && s.piece) set({ piece: null })
    else if (s.level === 'box' && s.room) get().goRoom(s.room)
    else get().goHouse()
  },
  setHover: code => set({ hoverBox: code }),
  setPeek: n => set({ peek: Math.max(0, n) }),
  setQuery: q => set({ query: q }),
  toggleQueued: code => set(s => ({
    queued: s.queued.includes(code) ? s.queued.filter(c => c !== code) : [...s.queued, code],
  })),
}))
