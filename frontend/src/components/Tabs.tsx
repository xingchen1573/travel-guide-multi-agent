export function Tabs({ tabs, active, onChange, className = '' }: {
  tabs: Array<{ key: string; label: string; badge?: string | number }>
  active: string
  onChange: (key: string) => void
  className?: string
}) {
  return (
    <div className={`flex gap-5 border-b border-border ${className}`}>
      {tabs.map((tab) => (
        <button
          key={tab.key}
          type="button"
          onClick={() => onChange(tab.key)}
          className={`pb-2.5 text-[13.5px] font-semibold relative top-px border-b-2 flex items-center gap-1.5 ${
            active === tab.key ? 'text-brand-dark border-brand' : 'text-ink-3 border-transparent hover:text-ink-2'
          }`}
        >
          {tab.label}
          {tab.badge !== undefined && tab.badge !== '' && (
            <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold ${active === tab.key ? 'bg-brand-soft text-brand-dark' : 'bg-bg-inset text-ink-3'}`}>{tab.badge}</span>
          )}
        </button>
      ))}
    </div>
  )
}
