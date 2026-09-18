// 线条SVG图标集，从design/admin_console/icons.js移植（同一套视觉语言），
// 这里改成参数化的React组件而不是字符串拼接。

const PATHS: Record<string, string> = {
  compass:
    '<circle cx="12" cy="12" r="9"/><path d="M14.8 9.2l-2 5.6-5.6 2 2-5.6z"/>',
  grid: '<rect x="3" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.5"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.5"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.5"/>',
  layers: '<path d="M12 3.5 3 8l9 4.5 9-4.5z"/><path d="M3 12.5 12 17l9-4.5M3 16.5 12 21l9-4.5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  database:
    '<ellipse cx="12" cy="5.5" rx="7.5" ry="2.8"/><path d="M4.5 5.5V18c0 1.55 3.36 2.8 7.5 2.8s7.5-1.25 7.5-2.8V5.5"/><path d="M4.5 11.75c0 1.55 3.36 2.8 7.5 2.8s7.5-1.25 7.5-2.8"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M20 20l-4.8-4.8"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  chevronRight: '<path d="M9 5l7 7-7 7"/>',
  chevronDown: '<path d="M5 9l7 7 7-7"/>',
  mapPin: '<path d="M12 21s-7-6.3-7-11.5A7 7 0 0 1 19 9.5C19 14.7 12 21 12 21z"/><circle cx="12" cy="9.5" r="2.3"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/>',
  check: '<path d="M5 13l4.5 4.5L19 8"/>',
  alertTriangle: '<path d="M12 4.2 21 19.5H3z"/><path d="M12 10v4.2M12 17v.01"/>',
  refresh: '<path d="M4 12a8 8 0 0 1 14-5.2M20 12a8 8 0 0 1-14 5.2"/><path d="M18.5 3.5V7H15M5.5 20.5V17H9"/>',
  link: '<path d="M9.5 14.5 14.5 9.5"/><path d="M11 6.5l1-1a3.6 3.6 0 0 1 5 5l-1 1M13 17.5l-1 1a3.6 3.6 0 0 1-5-5l1-1"/>',
  shirt: '<path d="M8.5 3.5 5 6.2 2.5 9.7 6 12l1.5-1.7V21h9V10.3L18 12l3.5-2.3L19 6.2l-3.5-2.7c-.6 1.2-1.9 2-3.5 2s-2.9-.8-3.5-2z"/>',
  video: '<rect x="3" y="6" width="12" height="12" rx="2"/><path d="M15 10l6-3v10l-6-3z"/>',
  music: '<path d="M10 17.5a2.5 2.5 0 1 1-2.5-2.5"/><path d="M10 17.5V4.5l9-2V13"/><path d="M19 13a2.5 2.5 0 1 1-2.5-2.5"/>',
  filter: '<path d="M4 5h16M7 12h10M10.5 19h3"/>',
  externalLink:
    '<path d="M9 5H5.5A1.5 1.5 0 0 0 4 6.5v12A1.5 1.5 0 0 0 5.5 20h12a1.5 1.5 0 0 0 1.5-1.5V15M14 4h6v6M20 4l-9.5 9.5"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  fileText:
    '<path d="M6 3.5h8l4 4V20a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4.5a1 1 0 0 1 1-1z"/><path d="M14 3.5V8h4"/><path d="M8.5 12.5h7M8.5 15.5h7M8.5 18h4.5"/>',
  trash:
    '<path d="M4 7h16"/><path d="M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7"/><path d="M6 7l1 13.5A1.5 1.5 0 0 0 8.5 22h7a1.5 1.5 0 0 0 1.5-1.5L18 7"/><path d="M10 11v7M14 11v7"/>',
  experiment:
    '<path d="M9 3h6M10 3v5l-5.5 9.3A2.5 2.5 0 0 0 6.7 21h10.6a2.5 2.5 0 0 0 2.2-3.7L14 8V3"/><path d="M7.5 15h9"/>',
}

export type IconName = keyof typeof PATHS

export function Icon({ name, className = 'w-4 h-4' }: { name: IconName; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      dangerouslySetInnerHTML={{ __html: PATHS[name] }}
    />
  )
}
