import { useEffect, useRef, useState } from "react";
import { pushMic, pushPcm } from "./audio.js";

function sessionFrom(raw) {
  if (!raw) return null;
  try {
    const data = JSON.parse(raw);
    if (data && typeof data === "object") return normalizeSession(data);
  } catch {
    /* a bare access token */
  }
  return { accessToken: raw };
}

function normalizeSession(data) {
  return {
    accessToken: data.accessToken || data.access_token || "",
    callId: data.callId || data.call_id || undefined,
    transport: data.transport || undefined,
    iceServers: data.iceServers || data.ice_servers || undefined,
  };
}

function readLocation() {
  const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
  const query = new URLSearchParams(location.search);
  return {
    dealer: query.get("dealer") || hash.get("dealer") || "",
    session: sessionFrom(hash.get("session") || hash.get("token")),
    preview: query.get("preview"),
  };
}

function turnsOf(update) {
  if (!update) return null;
  if (Array.isArray(update.transcript)) return update.transcript;
  if (Array.isArray(update.transcript_object)) return update.transcript_object;
  return null;
}

function humanError(err) {
  const msg = String(err?.message || err || "");
  if (/notallowed|permission|denied/i.test(msg)) {
    return "The microphone is blocked. Allow it for this page and try again.";
  }
  if (/token|expired|401|403|unauthorized/i.test(msg)) {
    return "This call pass has already been used. Run webcall again for a fresh one.";
  }
  return msg || "The call didn't start.";
}

function fmt(seconds) {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function useCall(audio) {
  // Read once. A new object every render would refetch the session.
  const [located] = useState(readLocation);
  const [phase, setPhase] = useState(() => {
    if (located.preview === "speak") return "speaking";
    if (located.preview === "listen") return "listening";
    return "idle";
  });
  const [dealer] = useState(located.dealer);
  const [status, setStatus] = useState(() => {
    if (located.preview === "speak") return "";
    if (located.preview === "listen") return "Listening";
    return "Tap the microphone and just start talking.";
  });
  const [statusBad, setStatusBad] = useState(false);
  const [line, setLine] = useState(() =>
    located.preview === "speak"
      ? { who: "Frontdesk", text: "Thursday morning is open. I can book that." }
      : { who: "", text: "How can I help you today?" },
  );
  const [turns, setTurns] = useState(() =>
    located.preview === "speak"
      ? [
          { role: "user", content: "Can I bring the car in Thursday morning?" },
          { role: "agent", content: "Thursday morning is open. I can book that." },
        ]
      : [],
  );
  const [clock, setClock] = useState(located.preview ? "1:24" : "0:00");
  const [muted, setMutedState] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const phaseRef = useRef(phase);
  const sessionRef = useRef(located.session);
  const companionRef = useRef(null);
  const clientRef = useRef(null);
  const startedAt = useRef(0);
  const micStop = useRef(null);
  phaseRef.current = phase;
  audio.current.phase = phase;

  useEffect(() => {
    if (located.session || located.preview) return undefined;
    let cancelled = false;
    fetch("/call.json", { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!cancelled && data) companionRef.current = normalizeSession(data);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [located.session, located.preview]);

  useEffect(() => {
    if (!dealer) return;
    document.title = `Frontdesk — ${dealer}`;
  }, [dealer]);

  useEffect(() => {
    if (phase !== "listening" && phase !== "speaking") return undefined;
    const id = setInterval(() => {
      if (!startedAt.current) return;
      setClock(fmt(Math.floor((Date.now() - startedAt.current) / 1000)));
    }, 500);
    return () => clearInterval(id);
  }, [phase]);

  useEffect(() => {
    return () => {
      micStop.current?.();
      try {
        clientRef.current?.stopCall();
      } catch {
        /* already closed */
      }
    };
  }, []);

  function applyTurns(items) {
    setTurns(items);
    const last = items[items.length - 1];
    const said = last && (last.content || last.text);
    const live = ["listening", "speaking", "connecting"].includes(phaseRef.current);
    if (said && live) {
      setLine({ who: last.role === "agent" ? "Frontdesk" : "You", text: said });
    }
  }

  function stopMic() {
    micStop.current?.();
    micStop.current = null;
    audio.current.micLive = false;
  }

  async function startMic() {
    stopMic();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      const ctx = new AudioContext();
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.72;
      source.connect(analyser);
      const freq = new Uint8Array(analyser.frequencyBinCount);
      const time = new Uint8Array(analyser.fftSize);
      let raf = 0;
      const tick = () => {
        raf = requestAnimationFrame(tick);
        analyser.getByteFrequencyData(freq);
        analyser.getByteTimeDomainData(time);
        pushMic(audio.current, freq, time);
      };
      tick();
      micStop.current = () => {
        cancelAnimationFrame(raf);
        stream.getTracks().forEach((track) => track.stop());
        ctx.close().catch(() => {});
        audio.current.micLive = false;
      };
    } catch {
      audio.current.micLive = false;
    }
  }

  function bind(client) {
    client.on("call_started", () => {
      setPhase("listening");
      setStatus("Listening");
      setStatusBad(false);
      audio.current.modeTarget = 1;
    });
    client.on("call_ready", () => {
      if (phaseRef.current === "connecting") {
        setPhase("listening");
        setStatus("Listening");
        audio.current.modeTarget = 1;
      }
    });
    client.on("agent_start_talking", () => {
      setPhase("speaking");
      setStatus("");
      audio.current.modeTarget = 2;
    });
    client.on("agent_stop_talking", () => {
      setPhase("listening");
      setStatus("Listening");
      audio.current.modeTarget = 1;
      audio.current.targetAmp = 0;
    });
    client.on("call_ended", () => {
      if (phaseRef.current === "ended") return;
      finish("ended");
    });
    client.on("error", (err) => {
      stopMic();
      audio.current.modeTarget = 0;
      setPhase("error");
      setStatus(humanError(err));
      setStatusBad(true);
    });
    client.on("update", (update) => {
      const items = turnsOf(update);
      if (items) applyTurns(items);
    });
    client.on("audio", (samples) => {
      if (samples?.length) pushPcm(audio.current, samples);
    });
  }

  function finish(next) {
    if (phaseRef.current === "ended") return;
    phaseRef.current = next;
    stopMic();
    audio.current.modeTarget = 0;
    audio.current.targetAmp = 0;
    const duration = startedAt.current
      ? fmt(Math.floor((Date.now() - startedAt.current) / 1000))
      : "0:00";
    setClock(duration);
    setPhase(next);
    setMutedState(false);
    setLine({ who: "Frontdesk", text: "That's everything." });
    setStatus(duration === "0:00" ? "Call ended." : `Call ended · ${duration}`);
    setStatusBad(false);
  }

  async function begin(typed) {
    if (located.preview) return;
    const next =
      (typed ? sessionFrom(typed) : null) || sessionRef.current || companionRef.current;
    if (!next?.accessToken) {
      setSheetOpen(true);
      return;
    }
    sessionRef.current = next;
    setSheetOpen(false);
    setPhase("connecting");
    setStatus("Connecting…");
    setStatusBad(false);
    audio.current.modeTarget = 0;
    try {
      if (!clientRef.current) {
        const { RetellWebClient } = await import("retell-client-js-sdk");
        const client = new RetellWebClient();
        bind(client);
        clientRef.current = client;
      }
      const config = { accessToken: next.accessToken, emitRawAudioSamples: true };
      if (next.callId) config.callId = next.callId;
      if (next.transport) config.transport = next.transport;
      if (next.iceServers?.length) config.iceServers = next.iceServers;
      await clientRef.current.startCall(config);
      startedAt.current = Date.now();
      setClock("0:00");
      setPhase("listening");
      setStatus("Listening");
      audio.current.modeTarget = 1;
      startMic();
    } catch (err) {
      stopMic();
      audio.current.modeTarget = 0;
      setPhase("error");
      setStatus(humanError(err));
      setStatusBad(true);
    }
  }

  function endCall() {
    if (located.preview) return;
    try {
      clientRef.current?.stopCall();
    } catch {
      /* already closed */
    }
    finish("ended");
  }

  function toggleMute() {
    const next = !muted;
    setMutedState(next);
    try {
      next ? clientRef.current?.mute() : clientRef.current?.unmute();
    } catch {
      /* not in a call */
    }
  }

  const live = phase === "listening" || phase === "speaking";

  return {
    phase,
    dealer,
    status,
    statusBad,
    line,
    turns,
    clock,
    muted,
    live,
    sheetOpen,
    setSheetOpen,
    drawerOpen,
    setDrawerOpen,
    preview: located.preview,
    begin,
    endCall,
    toggleMute,
  };
}
