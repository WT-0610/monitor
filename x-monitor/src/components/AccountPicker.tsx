import { useEffect, useRef, useState } from "react";
import type { Account } from "../data/types";

interface Props {
  accounts: Account[];
  value: string;
  onChange: (value: string) => void;
  allValue?: string;
  label?: string;
}

/** 三个媒体/内容页面共用的可搜索账号选择器。 */
export function AccountPicker({ accounts, value, onChange, allValue = "all", label = "账号" }: Props) {
  const [query, setQuery] = useState("");
  const picker = useRef<HTMLDetailsElement>(null);
  const selected = accounts.find((account) => account.id === value);
  const q = query.trim().toLocaleLowerCase();
  const matches = accounts.filter((account) =>
    !q || [account.name, account.handle, account.uid].some((text) => text.toLocaleLowerCase().includes(q))
  );

  useEffect(() => {
    const outside = (event: PointerEvent) => {
      if (!picker.current?.contains(event.target as Node)) picker.current!.open = false;
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && picker.current) picker.current.open = false;
    };
    document.addEventListener("pointerdown", outside);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("keydown", escape);
    };
  }, []);

  const choose = (next: string) => {
    onChange(next);
    setQuery("");
    if (picker.current) picker.current.open = false;
  };

  return (
    <details className="media-account-picker" ref={picker}>
      <summary title={selected?.name ?? "全部账号"} aria-label={`${label}：${selected?.name ?? "全部账号"}`}>
        {label}：{selected?.name ?? "全部账号"}<span aria-hidden="true">⌄</span>
      </summary>
      <div className="media-account-menu">
        <input type="search" value={query} onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索名称、@用户名或 UID" aria-label="搜索账号" />
        <div className="media-account-list">
          <button type="button" className={value === allValue ? "on" : ""} onClick={() => choose(allValue)}>全部账号</button>
          {matches.map((account) => (
            <button type="button" key={`${account.platform}-${account.id}`}
              className={value === account.id ? "on" : ""} onClick={() => choose(account.id)}
              title={account.uid}>{account.name}</button>
          ))}
          {matches.length === 0 && <span className="media-account-empty">没有匹配的账号</span>}
        </div>
      </div>
    </details>
  );
}
