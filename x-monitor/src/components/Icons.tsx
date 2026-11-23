const base = {
  width: 15,
  height: 15,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.7,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

export const IconFavorite = () => (
  <svg {...base} aria-hidden>
    <path d="m12 2.8 2.8 5.7 6.3.9-4.55 4.45 1.08 6.28L12 17.2l-5.63 2.93 1.08-6.28L2.9 9.4l6.3-.9z" />
  </svg>
);

export const IconOverview = () => (
  <svg {...base} aria-hidden>
    <rect x="3" y="3" width="7.5" height="7.5" rx="1.6" />
    <rect x="13.5" y="3" width="7.5" height="7.5" rx="1.6" />
    <rect x="3" y="13.5" width="7.5" height="7.5" rx="1.6" />
    <rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.6" />
  </svg>
);

export const IconFlow = () => (
  <svg {...base} aria-hidden>
    <path d="M4 6h16M4 12h16M4 18h9" />
  </svg>
);

export const IconQuery = () => (
  <svg {...base} aria-hidden>
    <ellipse cx="12" cy="6" rx="7.6" ry="2.9" />
    <path d="M4.4 6v12c0 1.6 3.4 2.9 7.6 2.9s7.6-1.3 7.6-2.9V6" />
    <path d="M4.4 12c0 1.6 3.4 2.9 7.6 2.9s7.6-1.3 7.6-2.9" />
  </svg>
);

export const IconMedia = () => (
  <svg {...base} aria-hidden>
    <rect x="3" y="4.5" width="18" height="15" rx="2.2" />
    <circle cx="8.5" cy="10" r="1.4" />
    <path d="M20.5 16.2 15.8 11.5 9.5 17.8" />
  </svg>
);

export const IconAccounts = () => (
  <svg {...base} aria-hidden>
    <circle cx="9.2" cy="8.4" r="3.2" />
    <path d="M3.4 19.6c0-3.1 2.6-5.1 5.8-5.1s5.8 2 5.8 5.1" />
    <path d="M16.2 5.6a3 3 0 0 1 0 5.6" />
    <path d="M17.6 14.9c1.9.6 3 2.1 3 4.7" />
  </svg>
);

export const IconSummary = () => (
  <svg {...base} aria-hidden>
    <path d="M3 20.2h18" />
    <path d="M6.5 20.2v-6.4M11.8 20.2V7.4M17.2 20.2v-9.1" />
  </svg>
);

export const IconLogs = () => (
  <svg {...base} aria-hidden>
    <rect x="3" y="4.5" width="18" height="15" rx="2.2" />
    <path d="M7.4 9.6 10.2 12l-2.8 2.4M13 14.6h3.6" />
  </svg>
);

export const IconSettings = () => (
  <svg {...base} aria-hidden>
    <circle cx="12" cy="12" r="3.1" />
    <path d="M19.3 14.9a1.5 1.5 0 0 0 .3 1.7l.1.1a1.9 1.9 0 1 1-2.7 2.7l-.1-.1a1.5 1.5 0 0 0-1.7-.3 1.5 1.5 0 0 0-.9 1.4v.2a1.9 1.9 0 1 1-3.8 0v-.1a1.5 1.5 0 0 0-.9-1.4 1.5 1.5 0 0 0-1.7.3l-.1.1a1.9 1.9 0 1 1-2.7-2.7l.1-.1a1.5 1.5 0 0 0 .3-1.7 1.5 1.5 0 0 0-1.4-.9H3.8a1.9 1.9 0 1 1 0-3.8H4a1.5 1.5 0 0 0 1.4-.9 1.5 1.5 0 0 0-.3-1.7l-.1-.1a1.9 1.9 0 1 1 2.7-2.7l.1.1a1.5 1.5 0 0 0 1.7.3H9.6a1.5 1.5 0 0 0 .9-1.4V3.8a1.9 1.9 0 1 1 3.8 0V4a1.5 1.5 0 0 0 .9 1.4 1.5 1.5 0 0 0 1.7-.3l.1-.1a1.9 1.9 0 1 1 2.7 2.7l-.1.1a1.5 1.5 0 0 0-.3 1.7v.1a1.5 1.5 0 0 0 1.4.9h.2a1.9 1.9 0 1 1 0 3.8h-.2a1.5 1.5 0 0 0-1.4.9z" />
  </svg>
);

export const IconTraffic = () => (
  <svg {...base} aria-hidden>
    <path d="M7.5 18.5V9.8M7.5 9.8 4.6 12.7M7.5 9.8l2.9 2.9" />
    <path d="M16.5 5.5v8.7M16.5 14.2l2.9-2.9M16.5 14.2l-2.9-2.9" />
  </svg>
);

/** 转关记录：转发出去的箭头 + 回勾 */
export const IconAction = () => (
  <svg {...base} aria-hidden>
    <path d="M4 9h11a4.4 4.4 0 0 1 0 8.8H9.4" />
    <path d="M7.4 5.6 4 9l3.4 3.4" />
    <path d="M12.2 17.8 9.6 15.2" />
  </svg>
);

/** 发现原博主：雷达扫描 */
export const IconDiscover = () => (
  <svg {...base} aria-hidden>
    <circle cx="12" cy="12" r="8.4" />
    <circle cx="12" cy="12" r="3.2" />
    <path d="M12 3.6v3.6M12 16.8v3.6M3.6 12H7.2M16.8 12h3.6" />
  </svg>
);

export const IconTrash = () => (
  <svg {...base} aria-hidden>
    <path d="M4.5 7h15" />    <path d="M9.5 7V4.9h5V7" />
    <path d="M6.4 7l.8 12.1a1.4 1.4 0 0 0 1.4 1.3h6.8a1.4 1.4 0 0 0 1.4-1.3L18.6 7" />
    <path d="M10.4 10.6v6.6M13.6 10.6v6.6" />
  </svg>
);

export const IconSearch = () => (
  <svg {...base} aria-hidden>
    <circle cx="11" cy="11" r="6.6" />
    <path d="m16 16 4.2 4.2" />
  </svg>
);

export const IconPlus = () => (
  <svg {...base} aria-hidden>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

export const IconRefresh = () => (
  <svg {...base} aria-hidden>
    <path d="M20.4 12a8.4 8.4 0 1 1-2.6-6.1" />
    <path d="M20.6 4.4v5.2h-5.2" />
  </svg>
);

export const IconFolder = () => (
  <svg {...base} aria-hidden>
    <path d="M3.5 7.4a2 2 0 0 1 2-2h3.4l1.9 2.2h7.7a2 2 0 0 1 2 2v7.6a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2z" />
  </svg>
);

export const IconExternal = () => (
  <svg {...base} aria-hidden>
    <path d="M14 4.5h5.5V10" />
    <path d="M19.5 4.5 11 13" />
    <path d="M18 14.4v4.1a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4" />
  </svg>
);
