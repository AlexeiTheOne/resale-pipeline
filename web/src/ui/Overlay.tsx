import { useEffect, useMemo, useRef, useState } from 'react'
import { CATEGORY, money, photoFor, profit, search, type Category } from '../data/derive'
import { ix, useStore } from '../store'
import { ordered, STATUS_LABEL } from '../three/Cards'

export function Overlay() {
  return (
    <>
      <TopBar />
      <Panel />
      <Legend />
      <PeekControl />
    </>
  )
}

function TopBar() {
  const { level, room, box, piece, goHouse, goRoom, back } = useStore()
  const r = room ? ix.room.get(room) : null
  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand-mark">▦</span>
        <span>Stockroom</span>
      </div>
      <nav className="crumbs">
        {level !== 'house' && <button className="back" onClick={back} aria-label="Back">‹</button>}
        <button className={level === 'house' ? 'on' : ''} onClick={goHouse}>House</button>
        {r && <><i>›</i><button className={level === 'room' ? 'on' : ''} onClick={() => goRoom(r.code)}>{r.name}</button></>}
        {box && <><i>›</i><button className={!piece ? 'on' : ''} onClick={() => useStore.getState().selectPiece(null)}>{box}</button></>}
        {piece && <><i>›</i><span className="on">{piece}</span></>}
      </nav>
      <Search />
      <Alerts />
    </header>
  )
}

function Search() {
  const query = useStore(s => s.query)
  const setQuery = useStore(s => s.setQuery)
  const goTo = useStore(s => s.goTo)
  const [focus, setFocus] = useState(false)
  const hits = useMemo(() => search(ix, query), [query])
  const boxes = new Set(hits.map(h => h.box))
  const input = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key === '/' && document.activeElement !== input.current) { e.preventDefault(); input.current?.focus() }
      if (e.key === 'Escape') { setQuery(''); input.current?.blur() }
    }
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [setQuery])

  const choose = (box: string, piece?: string) => {
    goTo(box, piece ?? null)
    setFocus(false)
    input.current?.blur()
  }
  return (
    <div className="search">
      <input
        ref={input}
        value={query}
        placeholder="Search code, box, UPC, brand…"
        onChange={e => setQuery(e.target.value)}
        onFocus={() => setFocus(true)}
        onBlur={() => setTimeout(() => setFocus(false), 150)}
        onKeyDown={e => { if (e.key === 'Enter' && hits[0]) choose(hits[0].box, hits[0].kind === 'piece' ? hits[0].piece : undefined) }}
      />
      {focus && query && (
        <div className="results">
          <div className="results-head">
            {hits.length ? `${hits.length} found in ${boxes.size} box${boxes.size === 1 ? '' : 'es'}` : 'Nothing at home matches'}
          </div>
          {hits.slice(0, 12).map(h => {
            if (h.kind === 'box') {
              const st = ix.boxStats.get(h.box)!
              return (
                <button key={h.box} className="hit" onMouseDown={() => choose(h.box)}>
                  <span className="hit-box" style={{ background: CATEGORY[st.category].color }}>{h.box}</span>
                  <span className="hit-text"><b>Box {h.box}</b><small>{st.count} items · {money(st.value)}</small></span>
                </button>
              )
            }
            const p = ix.piece.get(h.piece)!
            return (
              <button key={p.code} className="hit" onMouseDown={() => choose(p.box, p.code)}>
                <img src={photoFor(p)} alt="" />
                <span className="hit-text"><b>{p.name}</b><small>{p.code} · {STATUS_LABEL[p.status]} · {money(p.value)}</small></span>
                <span className="hit-where">{p.box}</span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

function Alerts() {
  const [open, setOpen] = useState(false)
  const goTo = useStore(s => s.goTo)
  const alerts = ix.inv.alerts
  return (
    <div className="alerts">
      <button className="bell" onClick={() => setOpen(o => !o)} aria-label="Sold alerts">
        💰<span className="badge">{alerts.length}</span>
      </button>
      {open && (
        <div className="alerts-pop">
          <div className="results-head">Sold — pull these</div>
          {alerts.map(a => {
            const p = ix.piece.get(a.piece)!
            return (
              <button key={a.piece} className="hit" onClick={() => { goTo(p.box, p.code); setOpen(false) }}>
                <img src={photoFor(p)} alt="" />
                <span className="hit-text"><b>{a.title}</b><small>{money(a.price)} · {a.when}</small></span>
                <span className="hit-where pull">Pull {a.piece}</span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}

function Stat({ k, v, hot }: { k: string; v: string; hot?: boolean }) {
  return <div className={`stat${hot ? ' hot' : ''}`}><b>{v}</b><span>{k}</span></div>
}

function Panel() {
  const { level, room, box, piece } = useStore()
  const [sheet, setSheet] = useState(false)
  // Phone: open the sheet when an item is picked, close it when you move on.
  useEffect(() => { setSheet(!!piece) }, [piece, level, box])
  return (
    <aside className={`panel${sheet ? ' up' : ''}`}>
      <button className="sheet-handle" onClick={() => setSheet(s => !s)} aria-label="Toggle panel"><span /></button>
      <div className="panel-scroll">
        {level === 'house' && <HousePanel />}
        {level === 'room' && room && <RoomPanel room={room} />}
        {level === 'box' && box && (piece ? <PiecePanel code={piece} /> : <BoxPanel box={box} />)}
      </div>
    </aside>
  )
}

function BoxRow({ code }: { code: string }) {
  const st = ix.boxStats.get(code)!
  const goTo = useStore(s => s.goTo)
  const setHover = useStore(s => s.setHover)
  const b = ix.box.get(code)!
  return (
    <button className="row" onClick={() => goTo(code)} onMouseEnter={() => setHover(code)} onMouseLeave={() => setHover(null)}>
      <span className="row-dot" style={{ background: CATEGORY[st.category].color }} />
      <span className="row-main"><b>{code}</b><small>{ix.room.get(b.room)!.name} · {st.count} items · {st.toPost} to post</small></span>
      <span className="row-num">{st.toPost ? `+${money(st.toPostProfit)}` : money(st.value)}</span>
    </button>
  )
}

const rankable = (c: Category) => c === 'hot' || c === 'low'
function listNext(codes: string[]) {
  return codes
    .filter(c => rankable(ix.boxStats.get(c)!.category))
    .sort((a, b) => ix.boxStats.get(b)!.toPostProfit - ix.boxStats.get(a)!.toPostProfit)
}

function HousePanel() {
  const h = ix.house
  const top = listNext(ix.inv.boxes.map(b => b.code)).slice(0, 6)
  return (
    <>
      <h2>Whole house</h2>
      <div className="stats">
        <Stat k="pieces" v={String(h.count)} />
        <Stat k="value" v={money(h.value)} />
        <Stat k="to post" v={String(h.toPost)} hot />
        <Stat k="profit waiting" v={money(h.toPostProfit)} hot />
      </div>
      <h3>List next</h3>
      <p className="muted">Boxes with the most unposted profit.</p>
      {top.map(c => <BoxRow key={c} code={c} />)}
    </>
  )
}

function RoomPanel({ room }: { room: 'G' | 'O' | 'S' | 'R' }) {
  const r = ix.room.get(room)!
  const st = ix.roomStats.get(room)!
  const codes = ix.inv.boxes.filter(b => b.room === room).map(b => b.code)
  const rest = codes.filter(c => !rankable(ix.boxStats.get(c)!.category))
  return (
    <>
      <h2>{r.name} <span className="pill">{room}</span></h2>
      <div className="stats">
        <Stat k="boxes" v={String(codes.length)} />
        <Stat k="pieces" v={String(st.count)} />
        <Stat k="to post" v={String(st.toPost)} hot />
        <Stat k="profit waiting" v={money(st.toPostProfit)} hot />
      </div>
      <h3>List next</h3>
      {listNext(codes).map(c => <BoxRow key={c} code={c} />)}
      <h3>Other boxes</h3>
      {rest.map(c => <BoxRow key={c} code={c} />)}
    </>
  )
}

function BoxPanel({ box }: { box: string }) {
  const st = ix.boxStats.get(box)!
  const b = ix.box.get(box)!
  const select = useStore(s => s.selectPiece)
  const queued = useStore(s => s.queued)
  return (
    <>
      <h2>Box {box} <span className="pill" style={{ background: CATEGORY[st.category].color }}>{CATEGORY[st.category].label}</span></h2>
      <p className="muted">{ix.room.get(b.room)!.name} · pile {b.pile.x + 1}, row {b.pile.z + 1} · level {b.level}</p>
      <div className="stats">
        <Stat k="pieces" v={String(st.count)} />
        <Stat k="value" v={money(st.value)} />
        <Stat k="to post" v={String(st.toPost)} hot />
        <Stat k="profit waiting" v={money(st.toPostProfit)} hot />
      </div>
      <h3>Inside</h3>
      {ordered(box).map(p => (
        <button key={p.code} className="row" onClick={() => select(p.code)}>
          <img className="row-img" src={photoFor(p)} alt="" />
          <span className="row-main"><b>{p.name}</b><small>{p.code} · {STATUS_LABEL[p.status]}{queued.includes(p.code) ? ' · ★ next' : ''}</small></span>
          <span className={`row-num ${p.status === 'not_posted' ? 'hot' : ''}`}>+{money(profit(p))}</span>
        </button>
      ))}
    </>
  )
}

function PiecePanel({ code }: { code: string }) {
  const p = ix.piece.get(code)!
  const queued = useStore(s => s.queued.includes(code))
  const toggle = useStore(s => s.toggleQueued)
  const select = useStore(s => s.selectPiece)
  return (
    <div className="piece">
      <img className="piece-img" src={photoFor(p)} alt="" />
      <h2>{p.name}</h2>
      <p className="muted">{p.code} · in box {p.box}{p.upc ? ` · UPC ${p.upc}` : ''}</p>
      <div className="stats">
        <Stat k={p.status === 'listed' ? 'listed at' : 'est. price'} v={money(p.value)} />
        <Stat k="cost" v={`$${p.cost.toFixed(2)}`} />
        <Stat k="est. profit" v={money(profit(p))} hot />
        <Stat k="status" v={STATUS_LABEL[p.status]} />
      </div>
      {p.status === 'not_posted' && (
        <button className={`cta${queued ? ' done' : ''}`} onClick={() => toggle(code)}>
          {queued ? '★ In the list-next queue' : 'List this next'}
        </button>
      )}
      {p.status === 'listed' && <button className="cta ghost" disabled>Open eBay listing</button>}
      <button className="link" onClick={() => select(null)}>← All items in {p.box}</button>
    </div>
  )
}

function Legend() {
  const level = useStore(s => s.level)
  const cats: Category[] = ['hot', 'low', 'backlog', 'listed']
  return (
    <div className={`legend lv-${level}`}>
      {cats.map(c => (
        <span key={c} title={CATEGORY[c].hint}><i style={{ background: CATEGORY[c].color }} />{CATEGORY[c].label}</span>
      ))}
    </div>
  )
}

function PeekControl() {
  const { level, room, peek, setPeek } = useStore()
  if (level !== 'room' || !room) return null
  const rows = Math.max(...ix.inv.boxes.filter(b => b.room === room).map(b => b.pile.z))
  return (
    <div className="peek">
      <span>See behind</span>
      <button onClick={() => setPeek(peek - 1)} disabled={peek === 0}>−</button>
      <b>{peek === 0 ? 'off' : `${peek} row${peek > 1 ? 's' : ''}`}</b>
      <button onClick={() => setPeek(Math.min(rows, peek + 1))} disabled={peek >= rows}>+</button>
    </div>
  )
}
