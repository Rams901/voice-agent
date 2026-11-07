import { Canvas } from "@react-three/fiber";
import { useEffect, useRef, useState } from "react";
import { createAudioBus } from "./audio.js";
import Orb from "./orb/Orb.jsx";
import { useCall } from "./useCall.js";

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path
        d="M12 3.5a3 3 0 0 0-3 3v5a3 3 0 0 0 6 0v-5a3 3 0 0 0-3-3Z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <path
        d="M6.5 11.5a5.5 5.5 0 0 0 11 0M12 17v3.2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor" />
    </svg>
  );
}

export default function App() {
  const audio = useRef(null);
  if (!audio.current) audio.current = createAudioBus();
  const call = useCall(audio);
  const [draft, setDraft] = useState("");
  const tokenRef = useRef(null);

  useEffect(() => {
    if (call.sheetOpen) tokenRef.current?.focus();
  }, [call.sheetOpen]);

  useEffect(() => {
    const onKey = (event) => {
      const typing =
        event.target instanceof HTMLTextAreaElement || event.target instanceof HTMLInputElement;
      if (event.key === "Escape") {
        if (call.sheetOpen) call.setSheetOpen(false);
        else call.setDrawerOpen(false);
        return;
      }
      if (typing) return;
      if (event.key === "t" || event.key === "T") call.setDrawerOpen((open) => !open);
      if ((event.key === "m" || event.key === "M") && call.live) call.toggleMute();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [call]);

  const dense = call.line.text.length > 90;
  const showClock = call.live || call.phase === "ended";

  return (
    <div className="room" data-phase={call.phase} data-drawer={call.drawerOpen ? "open" : "closed"}>
      <Canvas
        className="stage"
        dpr={[1, 2]}
        gl={{ antialias: true, alpha: true, powerPreference: "high-performance" }}
        camera={{ position: [0, 0, 8.35], fov: 28 }}
        onCreated={({ gl }) => gl.setClearColor(0x000000, 0)}
      >
        <Orb audio={audio} preview={call.preview} />
      </Canvas>

      <header className="top">
        <div className="brand">
          <span className="mark">Frontdesk</span>
          {call.dealer ? <span className="dealer">{call.dealer}</span> : null}
        </div>
        <time className={showClock ? "clock" : "clock idle"} dateTime={call.clock}>
          {showClock ? call.clock : "0:00"}
        </time>
        <button
          type="button"
          className="text-btn"
          aria-expanded={call.drawerOpen}
          onClick={() => call.setDrawerOpen((open) => !open)}
        >
          Transcript
          {call.turns.length > 0 ? <span className="count">{call.turns.length}</span> : null}
        </button>
      </header>

      <main className="copy">
        {call.line.who ? <p className="kicker">{call.line.who}</p> : <p className="kicker spacer" />}
        <h1 className={dense ? "dense" : undefined}>{call.line.text}</h1>
        <p className={call.statusBad ? "status bad" : "status"}>{call.status}</p>
        <div className="dock">
          {call.live ? (
            <button
              type="button"
              className={call.muted ? "round muted" : "round"}
              aria-pressed={call.muted}
              aria-label={call.muted ? "Unmute microphone" : "Mute microphone"}
              onClick={call.toggleMute}
            >
              <MicIcon />
            </button>
          ) : null}
          <button
            type="button"
            className={call.live ? "round end" : "round primary"}
            aria-label={call.live ? "End call" : "Start call"}
            disabled={call.phase === "connecting"}
            onClick={() => (call.live ? call.endCall() : call.begin())}
          >
            {call.live ? <StopIcon /> : <MicIcon />}
          </button>
        </div>
        <p className="hint">T transcript · M mute</p>
      </main>

      <aside className="drawer" aria-hidden={!call.drawerOpen}>
        <header>
          <h2>Transcript</h2>
          <button type="button" className="text-btn" onClick={() => call.setDrawerOpen(false)}>
            Close
          </button>
        </header>
        <div className="turns">
          {call.turns.length === 0 ? (
            <p className="empty">Nothing said yet.</p>
          ) : (
            call.turns.map((item, index) => {
              const role = item.role === "agent" ? "agent" : "user";
              return (
                <article key={`${index}-${role}`} className={`turn ${role}`}>
                  <div className="role">{role === "agent" ? "Frontdesk" : "You"}</div>
                  <p>{item.content || item.text || ""}</p>
                </article>
              );
            })
          )}
        </div>
      </aside>

      {call.sheetOpen ? (
        <div
          className="sheet"
          onClick={(event) => {
            if (event.target === event.currentTarget) call.setSheetOpen(false);
          }}
        >
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!draft.trim()) return;
              call.begin(draft.trim());
            }}
          >
            <h2>Start a call</h2>
            <p>
              Paste the access token from <code>python -m frontdesk.cli webcall</code>. A call
              opened from that command already carries its own session.
            </p>
            <textarea
              ref={tokenRef}
              spellCheck={false}
              autoComplete="off"
              placeholder="access token"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
            <div className="sheet-actions">
              <button type="button" className="text-btn" onClick={() => call.setSheetOpen(false)}>
                Cancel
              </button>
              <button type="submit" className="solid">
                Begin
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </div>
  );
}
