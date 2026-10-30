import { CATEGORY, money, photoFor, profit } from '../data/derive'
import { ix, useStore } from '../store'
import { pinEl } from '../three/anchors'
import { cardDelay, ordered, STATUS_LABEL } from '../three/Cards'

// DOM bits pinned to the 3D scene (see three/anchors.ts).
export function Labels() {
  return (
    <div className="labels">
      <RoomTags />
      <HoverTip />
      <CardLayer />
    </div>
  )
}

function RoomTags() {
  const level = useStore(s => s.level)
  const goRoom = useStore(s => s.goRoom)
  return (
    <>
      {ix.inv.rooms.map(r => {
        const st = ix.roomStats.get(r.code)!
        return (
          <div key={r.code} className="anchor" ref={pinEl(`room:${r.code}`)}>
            <button className={`room-tag${level === 'house' ? '' : ' gone'}`} onClick={() => goRoom(r.code)}>
              <span className="room-code">{r.code}</span>
              <span className="room-body">
                <b>{r.name}</b>
                <span>{st.count} items · {money(st.value)}</span>
                {st.toPost > 0 && <span className="room-hot" style={{ color: CATEGORY.hot.color }}>{st.toPost} to post</span>}
              </span>
            </button>
          </div>
        )
      })}
    </>
  )
}

function HoverTip() {
  const level = useStore(s => s.level)
  const code = useStore(s => s.hoverBox)
  const st = code ? ix.boxStats.get(code) : null
  const show = !!(code && st && level !== 'box')
  return (
    <div className="anchor" ref={pinEl('tip')}>
      <div className={`tip${show ? '' : ' gone'}`}>
        {show && (
          <>
            <b>{code}</b>
            <span>{st!.count} items · {money(st!.value)}</span>
            {st!.toPost > 0 && (
              <span className="tip-hot" style={{ color: CATEGORY[st!.category].color }}>
                {st!.toPost} to post · +{money(st!.toPostProfit)}
              </span>
            )}
          </>
        )}
      </div>
    </div>
  )
}

function CardLayer() {
  const level = useStore(s => s.level)
  const box = useStore(s => s.box)
  const selected = useStore(s => s.piece)
  const select = useStore(s => s.selectPiece)
  const queued = useStore(s => s.queued)
  if (level !== 'box' || !box) return null
  return (
    <>
      {ordered(box).map((p, i) => {
        const pr = profit(p)
        const sel = selected === p.code
        return (
          <div key={`${box}:${p.code}`} className="anchor" ref={pinEl(`card:${p.code}`, 2.4)}>
            <button
              className={`pcard s-${p.status}${sel ? ' sel' : ''}${selected && !sel ? ' dim' : ''}`}
              style={{ animationDelay: `${cardDelay(i)}ms` }}
              onClick={e => { e.stopPropagation(); select(sel ? null : p.code) }}
            >
              <img src={photoFor(p)} alt="" draggable={false} />
              {queued.includes(p.code) && <span className="pcard-star">★ next</span>}
              <span className="pcard-code">{p.code}</span>
              <span className="pcard-name">{p.name}</span>
              <span className="pcard-row">
                <span className={pr >= 25 ? 'pcard-profit good' : 'pcard-profit'}>+{money(pr)}</span>
                <span className="pcard-status">{STATUS_LABEL[p.status]}</span>
              </span>
            </button>
          </div>
        )
      })}
    </>
  )
}
