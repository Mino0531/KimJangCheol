import { urgentText } from './triage.js';

/** A conservative text-level guard; it cannot prove acoustic echo cancellation. */
const clean = (text) => String(text || '').normalize('NFKC').toLocaleLowerCase('ko')
  .replace(/[^\p{L}\p{N}]/gu, '');

function urgent(text, includeNumber = true, partial = false) {
  return urgentText(text, { includeNumber, partial });
}

function echo(text, reference) {
  if (!text || !reference) return false;
  return reference.includes(text) || (reference.length >= 4 && text.includes(reference));
}

function consentAnswer(text) {
  return /^(네|예|응|아니요|아니오|싫어요|거부|거부합니다|동의하지않습니다|동의하지않아요|동의안합니다|동의안해요)$/.test(text) ||
    /^(어+|음+|아+|네|예|응|그래요|그래)*동의(합니다|해요|해|할게요|하겠습니다|했어|했어요|했습니다)?$/.test(text);
}

// Split only on a complete, exact normalized playback prefix; preserve the
// user's original suffix rather than reconstructing it from normalized text.
function mixedSuffix(text, reference) {
  if (reference.length < 4) return '';
  const original = String(text || '').normalize('NFKC');
  let normalized = '', offset = 0, boundary = -1;
  for (const char of original) {
    const piece = clean(char);
    normalized += piece;
    offset += char.length;
    if (normalized.length === reference.length) boundary = offset;
  }
  if (!normalized.startsWith(reference) || boundary < 0) return '';
  const remainder = normalized.slice(reference.length);
  if (remainder.length < 3 || reference.includes(remainder)) return '';
  return original.slice(boundary).replace(/^[\s\p{P}\p{S}]+/gu, '').trim();
}

function partialEchoOverlap(value, reference) {
  if (!reference || value.length < 7) return false;
  for (let n = Math.min(reference.length, value.length - 3); n >= 4; n -= 1) {
    if (reference.includes(value.slice(0, n))) return true;
  }
  return false;
}

/** Tracks the recognition segments that began during speech playback. */
export class BargeGuard {
  constructor(now = () => Date.now()) {
    this.now = now;
    this.reset();
  }

  reset() {
    this.playback = null;
    this.recent = null;
    this.needsFreshConsent = false;
    this.rejectedEarlyConsent = false;
    this.retryCount = 0;
    this.retrySegment = null;
    this.retryClearlyDistinct = false;
    this.offeredConsentSegments = new Set();
    this.shortRetryCount = 0;
    this.shortRetrySegment = null;
    this.rejectedShortAnswer = false;
    this.offeredShortSegments = new Set();
    this.segments = new Map();
    this.origins = new Map();
    this.mixed = new Map();
  }

  begin({ text, protectedConsent = false, emergency = false, supported = true }) {
    this.playback = { reference: clean(text), protectedConsent, emergency, supported };
  }

  end() {
    if (this.playback) {
      this.recent = { ...this.playback, endedAt: this.now() };
      if (this.playback.protectedConsent) this.needsFreshConsent = true;
    }
    this.playback = null;
  }

  remember(segment, status) {
    this.segments.set(segment, status);
    if (this.segments.size > 256) {
      const oldest = this.segments.keys().next().value;
      this.segments.delete(oldest); this.origins.delete(oldest); this.mixed.delete(oldest);
    }
  }

  callerText(text, segment) {
    const mixed = this.mixed.get(segment);
    if (!mixed) {
      // A recognized playback-only partial must not become caller history
      // when combined with earlier pesticide context by the voice handler.
      if (this.segments.get(segment) === 'echo') return '';
      return String(text || '').trim();
    }
    const suffix = mixedSuffix(text, mixed.reference);
    if (suffix) return suffix;
    const value = clean(text), prior = clean(mixed.suffix);
    // A recognizer may revise its final result to exclude the echo altogether.
    // Accept only if the new result still contains the observed caller phrase.
    if (value.length >= 3 && prior.length >= 3 &&
        (value.startsWith(prior) || prior.startsWith(value)) && !mixed.reference.includes(value)) {
      return String(text || '').trim();
    }
    return '';
  }

  pureEcho(text, segment) {
    const value = clean(text);
    const origin = this.origins.get(segment) || this.playback ||
      (this.recent && this.now() - this.recent.endedAt < 1200 ? this.recent : null);
    return Boolean(value && origin?.reference && origin.reference.includes(value));
  }

  emergencyCallerText(text, segment) {
    const origin = this.origins.get(segment) || this.playback ||
      (this.recent && this.now() - this.recent.endedAt < 1200 ? this.recent : null);
    if (!origin?.emergency || !origin.reference) return '';
    const suffix = mixedSuffix(text, origin.reference);
    if (suffix) return suffix;
    const value = clean(text);
    if (!value || echo(value, origin.reference) || partialEchoOverlap(value, origin.reference)) return '';
    return String(text || '').trim();
  }

  takeConsentRetry() {
    if (!this.rejectedEarlyConsent || this.retryCount >= 2 ||
        this.offeredConsentSegments.has(this.retrySegment)) return false;
    this.rejectedEarlyConsent = false;
    this.offeredConsentSegments.add(this.retrySegment);
    this.retryCount += 1;
    return true;
  }

  takeConsentExhausted() {
    if (!this.rejectedEarlyConsent || this.retryCount < 2 ||
        this.offeredConsentSegments.has(this.retrySegment)) return false;
    // An answer indistinguishable from the guide might be acoustic echo;
    // never end a call merely because the guide was heard repeatedly.
    if (!this.retryClearlyDistinct) {
      this.rejectedEarlyConsent = false;
      return false;
    }
    this.rejectedEarlyConsent = false;
    this.offeredConsentSegments.add(this.retrySegment);
    return true;
  }

  noteEarlyConsent(segment, value) {
    if (consentAnswer(value) && !this.offeredConsentSegments.has(segment)) {
      const origin = this.origins.get(segment) || this.playback || this.recent;
      this.rejectedEarlyConsent = true;
      this.retrySegment = segment;
      this.retryClearlyDistinct = Boolean(origin?.reference && !echo(value, origin.reference));
    }
  }

  takeShortRetry() {
    if (!this.rejectedShortAnswer || this.shortRetryCount >= 2 ||
        this.offeredShortSegments.has(this.shortRetrySegment)) return false;
    this.rejectedShortAnswer = false;
    this.offeredShortSegments.add(this.shortRetrySegment);
    this.shortRetryCount += 1;
    return true;
  }

  takeShortExhausted() {
    if (!this.rejectedShortAnswer || this.shortRetryCount < 2 ||
        this.offeredShortSegments.has(this.shortRetrySegment)) return false;
    this.rejectedShortAnswer = false;
    this.offeredShortSegments.add(this.shortRetrySegment);
    return true;
  }

  noteShortAnswer(segment, value, origin) {
    if (!/^(네|예|응)$/.test(value) || !origin || origin.protectedConsent || origin.emergency ||
        !origin.supported || !origin.reference || echo(value, origin.reference) ||
        this.offeredShortSegments.has(segment)) return;
    this.rejectedShortAnswer = true;
    this.shortRetrySegment = segment;
  }

  partial(text, segment) {
    const value = clean(text);
    const saved = this.segments.get(segment);
    if (!value) return 'ignore';
    if (!this.playback) {
      if (saved === 'emergency_playback') return 'ignore';
      const origin = this.origins.get(segment);
      const suffix = origin ? mixedSuffix(text, origin.reference) : '';
      if (suffix && urgent(clean(suffix), true, true)) {
        this.mixed.set(segment, { reference: origin.reference, suffix });
        this.remember(segment, 'urgent');
        return 'urgent';
      }
      if (suffix && origin && !origin.protectedConsent && !origin.emergency && origin.supported) {
        this.mixed.set(segment, { reference: origin.reference, suffix });
        this.remember(segment, 'mixed');
        return saved === 'mixed' ? 'ignore' : 'interrupt';
      }
      const recentReference = this.recent && this.now() - this.recent.endedAt < 1200
        ? this.recent.reference : '';
      const echoed = saved === 'echo' || echo(value, recentReference);
      // Safety signals are acted on even without active playback. A pure 119
      // from recently played speech remains a suspected echo.
      if (urgent(value, !echoed, true)) {
        const reference = origin?.reference || recentReference;
        if (reference && (echo(value, reference) || partialEchoOverlap(value, reference))) {
          this.mixed.set(segment, { reference, suffix: '' });
        }
        this.remember(segment, 'urgent');
        return 'urgent';
      }
      if (echoed) {
        this.remember(segment, 'echo');
        return 'ignore';
      }
      // A new segment observed only after the full disclosure may answer it.
      // Segments started during disclosure remain blocked even if completed later.
      if (this.needsFreshConsent && saved === undefined) {
        const quiet = this.recent && this.now() - this.recent.endedAt >= 1200;
        this.remember(segment, quiet ? 'consent_fresh' : 'blocked');
      }
      return 'ignore';
    }
    const { reference, protectedConsent, emergency, supported } = this.playback;
    if (!this.origins.has(segment)) this.origins.set(segment, { ...this.playback });
    if (emergency) {
      this.remember(segment, 'emergency_playback');
      return 'ignore';
    }
    const suffix = mixedSuffix(text, reference);
    if (suffix && urgent(clean(suffix), true, true)) {
      this.mixed.set(segment, { reference, suffix });
      this.remember(segment, 'urgent');
      return 'urgent';
    }
    if (suffix && !protectedConsent && supported) {
      this.mixed.set(segment, { reference, suffix });
      this.remember(segment, 'mixed');
      return saved === 'mixed' ? 'ignore' : 'interrupt';
    }
    const echoed = echo(value, reference);
    // Danger words take precedence even if they overlap playback. Purely
    // echoed "119" is filtered, but indistinguishable danger speech may
    // still trigger on a loudspeaker: this is an intentional safety tradeoff.
    if (urgent(value, !echoed, true)) {
      if (reference && (echoed || partialEchoOverlap(value, reference))) {
        this.mixed.set(segment, { reference, suffix: '' });
      }
      this.remember(segment, 'urgent');
      return 'urgent';
    }
    if (echoed) {
      this.remember(segment, 'echo');
      return 'ignore';
    }
    if (partialEchoOverlap(value, reference)) {
      this.remember(segment, 'echo');
      return 'ignore';
    }
    if (protectedConsent || !supported || !reference || saved === 'echo' || saved === 'blocked') {
      this.remember(segment, saved === 'echo' ? 'echo' : 'blocked');
      return 'ignore';
    }
    if (value.length < 2) return 'ignore';
    if (saved === 'interrupted') return 'ignore';
    this.remember(segment, 'interrupted');
    return 'interrupt';
  }

  final(text, segment) {
    const value = clean(text);
    const saved = this.segments.get(segment);
    this.segments.delete(segment);
    if (!value) return false;
    if (saved === 'emergency_playback' || this.playback?.emergency) return false;
    const origin = this.origins.get(segment) || this.playback;
    const suffix = origin ? mixedSuffix(text, origin.reference) : '';
    if (suffix && urgent(clean(suffix))) this.mixed.set(segment, { reference: origin.reference, suffix });
    if (suffix && origin && !origin.protectedConsent && !origin.emergency && origin.supported) {
      this.mixed.set(segment, { reference: origin.reference, suffix });
      return true;
    }
    const echoReference = this.playback?.reference || this.recent?.reference || '';
    if (!suffix && urgent(value, !echo(value, echoReference)) && echoReference &&
        (echo(value, echoReference) || partialEchoOverlap(value, echoReference))) {
      this.mixed.set(segment, { reference: echoReference, suffix: '' });
    }
    if (urgent(value, !echo(value, echoReference))) return true;
    if (saved === 'urgent') return urgent(value);
    if (saved === 'echo' || saved === 'blocked') {
      if ((this.playback?.protectedConsent || this.needsFreshConsent) && consentAnswer(value)) {
        this.noteEarlyConsent(segment, value);
      }
      this.noteShortAnswer(segment, value, origin);
      return false;
    }
    if (saved === 'interrupted') return true;
    if (this.needsFreshConsent) {
      // Never accept a delayed, final-only segment as consent. Its speech may
      // have begun before disclosure was complete or come from loudspeaker echo.
      if (saved !== 'consent_fresh') {
        this.noteEarlyConsent(segment, value);
        return false;
      }
      this.needsFreshConsent = false;
    }
    if (this.playback) {
      const p = this.playback;
      if (p.protectedConsent) this.noteEarlyConsent(segment, value);
      if (value.length < 2) this.noteShortAnswer(segment, value, p);
      if (p.emergency || p.protectedConsent || !p.supported || !p.reference) return false;
      return value.length >= 2 && !echo(value, p.reference);
    }
    // Final-only provider results may arrive after playback ended. Fail closed
    // briefly after consent, and suppress identifiable echo of other prompts.
    if (this.recent && this.now() - this.recent.endedAt < 1200) {
      if (this.recent.protectedConsent || this.recent.emergency ||
          !this.recent.supported || echo(value, this.recent.reference)) return false;
    }
    return true;
  }
}
