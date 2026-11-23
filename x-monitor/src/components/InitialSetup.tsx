import { useState } from "react";
import { api } from "../data/api";
import type { Settings } from "../data/types";
import { WeiboLoginControl } from "./WeiboLoginControl";

type LoginPlatform = "weibo" | "x";

export function InitialSetup({ settings, onFinish, onSkip }: {
  settings: Settings;
  onFinish: (platform: LoginPlatform) => void;
  onSkip: () => void;
}) {
  const [platform, setPlatform] = useState<LoginPlatform | null>(null);
  const [step, setStep] = useState<"choose" | "login" | "account">("choose");
  const [loginReady, setLoginReady] = useState(false);
  const [account, setAccount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const hasSavedLogin = platform === "weibo" ? !!settings.cookie : !!settings.xCookie;

  const addAccount = async () => {
    if (!platform) return;
    const value = account.trim();
    const uid = platform === "weibo"
      ? (value.match(/(?:weibo\.com\/(?:u\/)?|^)(\d+)/i)?.[1] || "")
      : (value.match(/(?:x\.com|twitter\.com)\/([A-Za-z0-9_]{1,15})/i)?.[1] || value.replace(/^@/, ""));
    if (platform === "weibo" ? !/^\d+$/.test(uid) : !/^[A-Za-z0-9_]{1,15}$/.test(uid)) {
      setError(platform === "weibo" ? "输入微博 UID 或主页链接" : "输入 X 用户名或主页链接");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.addAccount(uid, platform);
      onFinish(platform);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "添加账号失败，请检查输入后重试");
    } finally { setBusy(false); }
  };

  return <main className="initial-setup">
    <section className="initial-setup-card" aria-label="首次使用引导">
      <div className="initial-setup-top"><span className="initial-setup-logo">桔</span><button className="secondary-btn" onClick={onSkip}>暂时跳过</button></div>
      <div className="initial-setup-progress">首次使用 · {step === "choose" ? "1" : step === "login" ? "2" : "3"} / 3</div>
      {step === "choose" && <>
        <h1>先连接一个平台</h1>
        <p className="initial-setup-hint">登录后添加要监控的账号，应用会在后台检查新内容。</p>
        <div className="initial-setup-choices">
          <button onClick={() => { setPlatform("weibo"); setStep("login"); }}><strong>微博</strong><span>在独立窗口登录微博</span><b>继续 →</b></button>
          <button onClick={() => { setPlatform("x"); setStep("login"); }}><strong>X</strong><span>在独立窗口登录 X</span><b>继续 →</b></button>
        </div>
      </>}
      {step === "login" && platform && <>
        <h1>登录{platform === "weibo" ? "微博" : " X"}</h1>
        <p className="initial-setup-hint">点击下方按钮，在打开的窗口完成登录。登录状态会保存到这台电脑。</p>
        <WeiboLoginControl key={platform} platform={platform} onReady={() => setLoginReady(true)} />
        <div className="initial-setup-actions"><button className="secondary-btn" onClick={() => setStep("choose")}>返回</button><button className="primary-btn" disabled={!loginReady && !hasSavedLogin} onClick={() => setStep("account")}>下一步</button></div>
        {hasSavedLogin && !loginReady && <small>检测到已保存的登录状态，可直接继续。</small>}
      </>}
      {step === "account" && platform && <>
        <h1>添加监控账号</h1>
        <p className="initial-setup-hint">输入要监控的{platform === "weibo" ? "微博 UID 或主页链接" : " X 用户名或主页链接"}。以后可在账号管理中继续添加。</p>
        <label className="initial-setup-field">{platform === "weibo" ? "微博账号" : "X 账号"}<input autoFocus value={account} onChange={(event) => { setAccount(event.target.value); setError(""); }} onKeyDown={(event) => { if (event.key === "Enter") void addAccount(); }} placeholder={platform === "weibo" ? "UID 或 weibo.com/u/..." : "@用户名 或 x.com/..."} /></label>
        {error && <p className="initial-setup-error" role="alert">{error}</p>}
        <div className="initial-setup-actions"><button className="secondary-btn" onClick={() => setStep("login")}>返回</button><button className="primary-btn" disabled={busy} onClick={() => void addAccount()}>{busy ? "添加中…" : "完成设置"}</button></div>
      </>}
    </section>
  </main>;
}
