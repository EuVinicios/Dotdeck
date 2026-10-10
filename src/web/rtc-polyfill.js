// Dotdeck: the page's voice calls run in Dotdeck's own WebRTC.
//
// The OpenDots voice call needs RTCPeerConnection, which distribution and Flatpak
// builds of WebKitGTK leave out. This script gives the page one that forwards every
// call to Dotdeck (src/core/voice.py), where GStreamer runs the real connection and
// plays/captures audio itself. The page keeps its own UI and logic unchanged.
//
// It also stands in for a native RTCPeerConnection when WebKit has one: Dotdeck's call
// controls (microphone and speaker choice, mute, the call in the window's header) only
// see calls that go through it, and on insecure origins (a LAN or Tailscale address
// over plain http) WebKit offers no microphone, so a native connection could not talk.
//
// Audio never goes through the page: getUserMedia() and the remote track hand the
// page silent placeholder streams, and their mute switches are relayed to GStreamer.
// Without navigator.mediaDevices (insecure origins) this script provides one for the
// microphone.
//
// SPDX-License-Identifier: GPL-3.0-or-later
(() => {
  'use strict';
  if (window.location.origin !== __DOTDECK_ORIGIN__ || window.__dotdeckVoiceEvent) return;
  const handlers = window.webkit && window.webkit.messageHandlers;
  const native = handlers && handlers.dotdeckVoice;
  if (!native) return;

  const call = async (op, args) => {
    const reply = await native.postMessage(JSON.stringify(Object.assign({ op }, args || {})));
    return reply ? JSON.parse(reply) : {};
  };

  const peers = new Map();
  Object.defineProperty(window, '__dotdeckVoiceEvent', {
    value: (pcId, json) => {
      const pc = peers.get(String(pcId));
      if (pc) pc._native(JSON.parse(json));
    },
  });

  let audioContext;
  const placeholderStream = () => {
    audioContext = audioContext || new AudioContext();
    if (audioContext.state === 'suspended') audioContext.resume().catch(() => {});
    return audioContext.createMediaStreamDestination().stream;
  };

  // WebKit can drop and recreate the JavaScript wrappers of media objects, so tracks
  // and streams are remembered by id, and their switches are hooked on the prototypes.
  const micTracks = new Map(); // microphone track id -> ids of the calls it was added to
  const remoteStreams = new Map(); // remote stream id -> call id

  const relayMic = (track, value) => {
    const calls = micTracks.get(track.id);
    if (!calls) return;
    for (const id of calls) call('setMicEnabled', { pc: id, enabled: value }).catch(() => {});
  };
  if (typeof MediaStreamTrack !== 'undefined') {
    const enabled = Object.getOwnPropertyDescriptor(MediaStreamTrack.prototype, 'enabled');
    if (enabled && enabled.set) {
      Object.defineProperty(MediaStreamTrack.prototype, 'enabled', {
        configurable: true,
        enumerable: enabled.enumerable,
        get() {
          return enabled.get.call(this);
        },
        set(value) {
          enabled.set.call(this, value);
          relayMic(this, Boolean(value));
        },
      });
    }
    const stopTrack = MediaStreamTrack.prototype.stop;
    MediaStreamTrack.prototype.stop = function () {
      relayMic(this, false);
      return stopTrack.call(this);
    };
  }

  const microphone = async () => {
    const result = await call('requestMicrophone');
    if (!result.allowed) throw new DOMException('Microphone access was denied.', 'NotAllowedError');
    const stream = placeholderStream();
    stream.getAudioTracks().forEach((track) => micTracks.set(track.id, new Set()));
    return stream;
  };
  const onlyMicrophone = (constraints) => Boolean(constraints && constraints.audio && !constraints.video);

  const mediaDevices = navigator.mediaDevices;
  if (mediaDevices && typeof mediaDevices.getUserMedia === 'function') {
    const original = mediaDevices.getUserMedia.bind(mediaDevices);
    mediaDevices.getUserMedia = async (constraints) =>
      onlyMicrophone(constraints) ? microphone() : original(constraints);
  } else {
    const devices = mediaDevices || new EventTarget();
    devices.getUserMedia = async (constraints) => {
      if (!onlyMicrophone(constraints))
        throw new DOMException('Only the microphone is available here.', 'NotSupportedError');
      return microphone();
    };
    if (typeof devices.enumerateDevices !== 'function') {
      const microphoneInfo = { deviceId: 'default', groupId: 'dotdeck', kind: 'audioinput', label: '' };
      devices.enumerateDevices = async () => [Object.assign({ toJSON: () => microphoneInfo }, microphoneInfo)];
    }
    if (typeof devices.getSupportedConstraints !== 'function') {
      devices.getSupportedConstraints = () => ({
        autoGainControl: true,
        deviceId: true,
        echoCancellation: true,
        noiseSuppression: true,
      });
    }
    if (!mediaDevices) {
      devices.ondevicechange = null;
      Object.defineProperty(Navigator.prototype, 'mediaDevices', {
        configurable: true,
        enumerable: true,
        get() {
          return devices;
        },
      });
    }
  }

  const callOf = (element) => {
    const stream = element.srcObject;
    return stream && typeof stream.id === 'string' ? remoteStreams.get(stream.id) : undefined;
  };

  // Muting or pausing the <audio> element that plays a call mutes the speakers in
  // GStreamer; playing it again brings them back unless it is still muted.
  const pausedCalls = new Set();
  // `paused` tells native the page paused the call audio, which OpenDots only does when
  // it hangs up: the call ends there instead of ~30 s later when the line drops.
  const speakers = (id, mute, paused = false) =>
    call('setSpeakerMuted', { pc: id, muted: mute, paused }).catch(() => {});
  const muted = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'muted');
  const isMuted = (element) => Boolean(muted && muted.get ? muted.get.call(element) : element.muted);
  if (muted && muted.set) {
    Object.defineProperty(HTMLMediaElement.prototype, 'muted', {
      configurable: true,
      enumerable: muted.enumerable,
      get() {
        return muted.get.call(this);
      },
      set(value) {
        muted.set.call(this, value);
        const id = callOf(this);
        if (id) speakers(id, Boolean(value) || pausedCalls.has(id));
      },
    });
  }

  const pause = HTMLMediaElement.prototype.pause;
  HTMLMediaElement.prototype.pause = function () {
    const id = callOf(this);
    if (id && !pausedCalls.has(id)) {
      pausedCalls.add(id);
      speakers(id, true, true);
    }
    return pause.call(this);
  };

  // The <audio> element of a call plays nothing itself: GStreamer plays the call.
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    const id = callOf(this);
    if (!id) return play.call(this);
    if (pausedCalls.delete(id)) speakers(id, isMuted(this));
    return Promise.resolve();
  };

  const fire = (target, event) => {
    const handler = target['on' + event.type];
    if (typeof handler === 'function') {
      try {
        handler.call(target, event);
      } catch (error) {
        setTimeout(() => {
          throw error;
        });
      }
    }
    target.dispatchEvent(event);
  };

  const plainEvent = (type, fields) => Object.assign(new Event(type), fields || {});

  class DotdeckDataChannel extends EventTarget {
    constructor(pc, label, ordered) {
      super();
      this._pc = pc;
      this._id = null;
      this.label = label;
      this.ordered = ordered;
      this.protocol = '';
      this.readyState = 'connecting';
      this.bufferedAmount = 0;
      this.bufferedAmountLowThreshold = 0;
      this.binaryType = 'blob';
      this.onopen = this.onmessage = this.onclose = this.onerror = null;
      this.onclosing = this.onbufferedamountlow = null;
    }

    send(data) {
      if (this.readyState !== 'open')
        throw new DOMException('The data channel is not open.', 'InvalidStateError');
      const id = this._id;
      this._pc._enqueue((pc) => call('send', { pc, dc: id, data: String(data) })).catch(() => {});
    }

    close() {
      if (this.readyState === 'closed' || this.readyState === 'closing') return;
      this.readyState = 'closing';
      const id = this._id;
      this._pc._enqueue((pc) => (id ? call('closeChannel', { pc, dc: id }) : null)).catch(() => {});
    }

    _event(event) {
      if (event.type === 'dcopen') {
        this.readyState = 'open';
        fire(this, plainEvent('open'));
      } else if (event.type === 'dcmessage') {
        fire(this, new MessageEvent('message', { data: event.data }));
      } else if (event.type === 'dcerror') {
        fire(this, plainEvent('error', { error: new Error(event.message) }));
      } else if (event.type === 'dcclose' && this.readyState !== 'closed') {
        this.readyState = 'closed';
        fire(this, plainEvent('close'));
      }
    }
  }

  const ICE_STATES = {
    new: 'new',
    connecting: 'checking',
    connected: 'connected',
    disconnected: 'disconnected',
    failed: 'failed',
    closed: 'closed',
  };

  class DotdeckPeerConnection extends EventTarget {
    constructor(configuration) {
      super();
      this._config = configuration || {};
      this._channels = new Map();
      this._closed = false;
      this._failure = null;
      this.connectionState = 'new';
      this.iceConnectionState = 'new';
      this.iceGatheringState = 'new';
      this.signalingState = 'stable';
      this.localDescription = null;
      this.remoteDescription = null;
      this.currentLocalDescription = null;
      this.currentRemoteDescription = null;
      this.onconnectionstatechange = this.oniceconnectionstatechange = null;
      this.onicegatheringstatechange = this.onsignalingstatechange = null;
      this.ontrack = this.ondatachannel = this.onicecandidate = this.onnegotiationneeded = null;
      // Operations run strictly in order, as the page issued them.
      this._id = null;
      this._queue = call('create').then((reply) => {
        this._id = reply.pc;
        peers.set(reply.pc, this);
        return reply.pc;
      });
    }

    _enqueue(operation) {
      const next = this._queue.then((id) => Promise.resolve(operation(id)).then(() => id));
      this._queue = next.catch(() => this._id);
      return next;
    }

    _request(op, args) {
      let result;
      return this._enqueue((pc) =>
        call(op, Object.assign({ pc }, args || {})).then((reply) => {
          result = reply;
        }),
      ).then(() => result);
    }

    createDataChannel(label, options) {
      const ordered = !options || options.ordered !== false;
      const channel = new DotdeckDataChannel(this, String(label), ordered);
      this._enqueue((pc) =>
        call('createDataChannel', { pc, label: channel.label, ordered }).then((reply) => {
          channel._id = reply.dc;
          this._channels.set(reply.dc, channel);
        }),
      ).catch((error) => {
        this._failure = this._failure || error;
      });
      return channel;
    }

    addTrack(track) {
      const calls = track && micTracks.get(track.id);
      if (!calls || track.kind !== 'audio')
        throw new DOMException('Dotdeck only carries the microphone in calls.', 'NotSupportedError');
      this._enqueue((pc) => {
        calls.add(pc);
        return call('addTrack', { pc, kind: 'audio' }).then(() =>
          track.enabled ? null : call('setMicEnabled', { pc, enabled: false }),
        );
      }).catch((error) => {
        this._failure = this._failure || error;
      });
      return { track, replaceTrack: async () => {}, getParameters: () => ({}) };
    }

    getSenders() {
      return [];
    }

    getReceivers() {
      return [];
    }

    getTransceivers() {
      return [];
    }

    async createOffer() {
      if (this._failure) throw this._failure;
      const reply = await this._request('createOffer');
      return { type: 'offer', sdp: reply.sdp };
    }

    async createAnswer() {
      const reply = await this._request('createAnswer');
      return { type: 'answer', sdp: reply.sdp };
    }

    async setLocalDescription(description) {
      if (!description) description = await this.createOffer();
      await this._request('setLocalDescription', { type: description.type, sdp: description.sdp });
      this.localDescription = this.currentLocalDescription = {
        type: description.type,
        sdp: description.sdp,
      };
      this._signaling(description.type === 'offer' ? 'have-local-offer' : 'stable');
    }

    async setRemoteDescription(description) {
      await this._request('setRemoteDescription', { type: description.type, sdp: description.sdp });
      this.remoteDescription = this.currentRemoteDescription = {
        type: description.type,
        sdp: description.sdp,
      };
      this._signaling(description.type === 'offer' ? 'have-remote-offer' : 'stable');
    }

    async addIceCandidate(candidate) {
      if (!candidate || !candidate.candidate) return;
      await this._request('addIceCandidate', {
        mline: candidate.sdpMLineIndex || 0,
        candidate: candidate.candidate,
      });
    }

    async getStats() {
      return new Map();
    }

    getConfiguration() {
      return this._config;
    }

    close() {
      if (this._closed) return;
      this._closed = true;
      this.connectionState = 'closed';
      this.iceConnectionState = 'closed';
      this.signalingState = 'closed';
      for (const channel of this._channels.values()) channel._event({ type: 'dcclose' });
      for (const calls of micTracks.values()) calls.delete(this._id);
      for (const [stream, id] of remoteStreams) if (id === this._id) remoteStreams.delete(stream);
      pausedCalls.delete(this._id);
      this._enqueue((pc) => call('close', { pc }))
        .catch(() => {})
        .finally(() => peers.delete(this._id));
    }

    _signaling(state) {
      if (this.signalingState === state) return;
      this.signalingState = state;
      fire(this, plainEvent('signalingstatechange'));
    }

    _native(event) {
      if (this._closed) return;
      switch (event.type) {
        case 'connectionstatechange': {
          if (event.state === this.connectionState) return;
          this.connectionState = event.state;
          this.iceConnectionState = ICE_STATES[event.state] || event.state;
          fire(this, plainEvent('iceconnectionstatechange'));
          fire(this, plainEvent('connectionstatechange'));
          break;
        }
        case 'mediaerror': {
          // The microphone or speakers failed: report it the way a dropped call looks.
          this._failure = new Error(event.message || 'The audio device failed.');
          this.connectionState = 'failed';
          this.iceConnectionState = 'failed';
          fire(this, plainEvent('iceconnectionstatechange'));
          fire(this, plainEvent('connectionstatechange'));
          break;
        }
        case 'track': {
          const stream = placeholderStream();
          remoteStreams.set(stream.id, this._id);
          const track = stream.getAudioTracks()[0];
          const receiver = { track };
          fire(this, plainEvent('track', { track, streams: [stream], receiver, transceiver: { receiver } }));
          break;
        }
        case 'icecandidate': {
          const candidate = { candidate: event.candidate, sdpMLineIndex: event.mline, sdpMid: null };
          fire(this, plainEvent('icecandidate', { candidate }));
          break;
        }
        case 'datachannel': {
          const channel = new DotdeckDataChannel(this, event.label || '', true);
          channel._id = event.dc;
          this._channels.set(event.dc, channel);
          fire(this, plainEvent('datachannel', { channel }));
          break;
        }
        case 'dcopen':
        case 'dcmessage':
        case 'dcerror':
        case 'dcclose': {
          const channel = this._channels.get(event.dc);
          if (channel) channel._event(event);
          break;
        }
        default:
          break;
      }
    }
  }

  class DotdeckSessionDescription {
    constructor(init) {
      this.type = init && init.type;
      this.sdp = (init && init.sdp) || '';
    }

    toJSON() {
      return { type: this.type, sdp: this.sdp };
    }
  }

  class DotdeckIceCandidate {
    constructor(init) {
      Object.assign(this, { candidate: '', sdpMid: null, sdpMLineIndex: 0 }, init || {});
    }

    toJSON() {
      return { candidate: this.candidate, sdpMid: this.sdpMid, sdpMLineIndex: this.sdpMLineIndex };
    }
  }

  window.RTCPeerConnection = DotdeckPeerConnection;
  window.RTCSessionDescription = DotdeckSessionDescription;
  window.RTCIceCandidate = DotdeckIceCandidate;
  window.RTCDataChannel = DotdeckDataChannel;
})();
