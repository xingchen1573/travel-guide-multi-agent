import { Link, NavLink, useLocation } from 'react-router-dom'
import { Icon } from './Icon'

export type ProjectParent = {
  to: string
  label: string
}

export type ProjectNavigationState = {
  projectParent?: ProjectParent
}

export function ProjectTabs({ projectId }: { projectId: string }) {
  const location = useLocation()
  const navigationState = location.state as ProjectNavigationState | null
  const projectBase = `/projects/${projectId}`
  const isOverview = location.pathname.replace(/\/$/, '') === projectBase
  const parent = navigationState?.projectParent ?? { to: '/', label: 'Project 列表' }
  const backTarget = isOverview ? parent : { to: projectBase, label: '项目概览' }
  const tabs = [
    { to: projectBase, label: '概览', end: true },
    { to: `/projects/${projectId}/outputs`, label: 'Agent 产出', end: false },
    { to: `/projects/${projectId}/poi_photos`, label: 'POI 图片', end: false },
    { to: `/projects/${projectId}/user_notes`, label: '用户备注', end: false },
    { to: `/projects/${projectId}/timeline`, label: '时间线', end: false },
    { to: `/projects/${projectId}/presentation`, label: '呈现层', end: false },
    { to: `/projects/${projectId}/review`, label: '质量中心', end: false },
  ]

  return (
    <div className="mb-5 space-y-3">
      <Link
        to={backTarget.to}
        state={isOverview ? undefined : navigationState}
        className="inline-flex items-center gap-1 text-xs font-medium text-ink-3 transition-colors hover:text-brand-dark"
      >
        <Icon name="chevronRight" className="h-3.5 w-3.5 rotate-180" />
        返回{backTarget.label}
      </Link>
      <div className="scroll-boundary-x flex gap-5 border-b border-border overflow-x-auto">
        {tabs.map((tab) => (
          <NavLink
            key={tab.to}
            to={tab.to}
            state={navigationState}
            end={tab.end}
            className={({ isActive }) =>
              `shrink-0 pb-2.5 text-[13.5px] font-semibold relative top-px border-b-2 ${
                isActive ? 'text-brand-dark border-brand' : 'text-ink-3 border-transparent'
              }`
            }
          >
            {tab.label}
          </NavLink>
        ))}
      </div>
    </div>
  )
}
