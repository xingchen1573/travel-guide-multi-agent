import { NavLink } from 'react-router-dom'
import { Icon, type IconName } from './Icon'

const NAV_ITEMS: { to: string; label: string; icon: IconName }[] = [
  { to: '/', label: '项目', icon: 'grid' },
  { to: '/trash', label: '回收站', icon: 'trash' },
  { to: '/materials', label: '素材库', icon: 'database' },
  { to: '/materials/insights', label: '素材洞察', icon: 'compass' },
  { to: '/model-config', label: '模型/Key', icon: 'refresh' },
  { to: '/feishu', label: '飞书运维', icon: 'link' },
  { to: '/review-center', label: '问题与改进', icon: 'check' },
  { to: '/experiments', label: '对照实验', icon: 'experiment' },
]

export function Sidebar({
  mobile = false,
  onNavigate,
  onClose,
}: {
  mobile?: boolean
  onNavigate?: () => void
  onClose?: () => void
}) {
  return (
    <aside
      className={`${
        mobile
          ? 'fixed inset-y-0 left-0 z-50 flex w-72 max-w-[85vw] shadow-xl'
          : 'hidden md:flex w-62 shrink-0 sticky top-0'
      } bg-surface border-r border-border flex-col h-screen`}
    >
      <div className="flex items-center gap-2.5 px-5 pt-5 pb-4">
        <div className="w-8 h-8 rounded-[9px] bg-linear-to-br from-brand to-gold flex items-center justify-center text-white font-bold text-[15px] shadow-sm shrink-0">
          旅
        </div>
        <div className="flex flex-col leading-tight">
          <b className="text-sm font-bold tracking-tight">攻略中枢</b>
          <span className="text-[11px] text-ink-3">多Agent运营后台</span>
        </div>
        {mobile && (
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭导航"
            title="关闭导航"
            className="ml-auto w-8 h-8 inline-flex items-center justify-center text-ink-2 hover:text-ink hover:bg-bg-inset rounded-md"
          >
            <Icon name="x" className="w-4.5 h-4.5" />
          </button>
        )}
      </div>

      <nav className="scroll-boundary-y flex-1 px-3 py-2 overflow-y-auto">
        <div className="text-[11px] font-semibold tracking-wide text-ink-3 uppercase px-2.5 pt-3.5 pb-1.5">
          工作台
        </div>
        {NAV_ITEMS.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.to === '/' || item.to === '/materials'}
            onClick={onNavigate}
            className={({ isActive }) =>
              `flex items-center gap-2.5 px-2.5 py-2.5 rounded-lg text-[13.5px] font-medium mb-0.5 transition-colors ${
                isActive
                  ? 'bg-brand-soft text-brand-dark font-semibold'
                  : 'text-ink-2 hover:bg-bg-inset hover:text-ink'
              }`
            }
          >
            <Icon name={item.icon} className="w-[17px] h-[17px] opacity-85" />
            <span>{item.label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="px-5 pt-3.5 pb-4.5 border-t border-border flex items-center gap-2.5">
        <div className="w-7 h-7 rounded-full bg-teal-soft text-teal flex items-center justify-center text-xs font-bold shrink-0">
          运
        </div>
        <div className="flex flex-col leading-tight">
          <b className="text-[12.5px] font-semibold">运营账号</b>
          <span className="text-[11px] text-ink-3">manage@trip-agent</span>
        </div>
      </div>
    </aside>
  )
}
