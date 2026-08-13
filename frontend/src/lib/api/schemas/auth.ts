import { z } from 'zod';
import type {
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
} from '@simplewebauthn/browser';
import { jsonValueSchema } from './common';

export const sentResponseSchema = z.looseObject({ sent: z.boolean() });

export const sessionUserSchema = z.looseObject({
  id: z.number(),
  email: z.string(),
  role: z.enum(['user', 'admin']),
});

export const passkeyCapabilitySchema = z.looseObject({
  available: z.boolean(),
  access_mode: z.string(),
});

const authenticatorTransportSchema = z.enum([
  'ble',
  'cable',
  'hybrid',
  'internal',
  'nfc',
  'smart-card',
  'usb',
]);

const credentialDescriptorSchema = z.looseObject({
  id: z.string(),
  type: z.literal('public-key'),
  transports: z.array(authenticatorTransportSchema).optional(),
});

const extensionsSchema = z.looseObject({
  appid: z.string().optional(),
  credProps: z.boolean().optional(),
  hmacCreateSecret: z.boolean().optional(),
  minPinLength: z.boolean().optional(),
});

export const passkeyCreationOptionsSchema: z.ZodType<PublicKeyCredentialCreationOptionsJSON> =
  z.looseObject({
    rp: z.looseObject({ name: z.string(), id: z.string().optional() }),
    user: z.looseObject({
      id: z.string(),
      name: z.string(),
      displayName: z.string(),
    }),
    challenge: z.string(),
    pubKeyCredParams: z.array(z.looseObject({
      alg: z.number(),
      type: z.literal('public-key'),
    })),
    timeout: z.number().optional(),
    excludeCredentials: z.array(credentialDescriptorSchema).optional(),
    authenticatorSelection: z.looseObject({
      authenticatorAttachment: z.enum(['cross-platform', 'platform']).optional(),
      requireResidentKey: z.boolean().optional(),
      residentKey: z.enum(['discouraged', 'preferred', 'required']).optional(),
      userVerification: z.enum(['discouraged', 'preferred', 'required']).optional(),
    }).optional(),
    hints: z.array(z.enum(['hybrid', 'security-key', 'client-device'])).optional(),
    attestation: z.enum(['direct', 'enterprise', 'indirect', 'none']).optional(),
    attestationFormats: z.array(z.enum([
      'fido-u2f',
      'packed',
      'android-safetynet',
      'android-key',
      'tpm',
      'apple',
      'none',
    ])).optional(),
    extensions: extensionsSchema.optional(),
  });

export const passkeyRequestOptionsSchema: z.ZodType<PublicKeyCredentialRequestOptionsJSON> =
  z.looseObject({
    challenge: z.string(),
    timeout: z.number().optional(),
    rpId: z.string().optional(),
    allowCredentials: z.array(credentialDescriptorSchema).optional(),
    userVerification: z.enum(['discouraged', 'preferred', 'required']).optional(),
    hints: z.array(z.enum(['hybrid', 'security-key', 'client-device'])).optional(),
    extensions: extensionsSchema.optional(),
  });

export const passkeyRegistrationResultSchema = z.looseObject({
  id: z.string(),
  nickname: z.string().nullable(),
});

export const passkeyInfoSchema = z.looseObject({
  id: z.string(),
  nickname: z.string().nullable(),
  transports: z.array(z.string()).nullable(),
  created_at: z.string().nullable(),
  last_used_at: z.string().nullable(),
});
export const passkeyInfoListSchema = z.array(passkeyInfoSchema);

export const passkeyCountSchema = z.looseObject({ count: z.number() });

export const adminUserSchema = z.looseObject({
  id: z.number(),
  email: z.string(),
  role: z.enum(['user', 'admin']),
  created_at: z.string(),
  last_login_at: z.string().nullable(),
  deleted_at: z.string().nullable().optional(),
  invite_link: z.string().nullable().optional(),
  is_owner: z.boolean().optional(),
  owner_source: z.enum(['none', 'database', 'environment']).nullable().optional(),
  owner_state: z.enum([
    'missing',
    'invalid_value',
    'missing_or_deleted_user',
    'non_admin_user',
    'valid',
  ]).nullable().optional(),
});
export const adminUserListSchema = z.array(adminUserSchema);

export const ownerIdentitySchema = z.looseObject({
  source: z.enum(['database', 'environment']),
  state: z.literal('valid'),
  user_id: z.number(),
});

export const sendSignInLinkSchema = z.looseObject({
  sent: z.boolean(),
  sent_link: z.string().nullable().optional(),
});

export const auditLogEntrySchema = z.looseObject({
  id: z.number(),
  user_id: z.string().nullable(),
  action: z.string(),
  resource: z.string(),
  metadata: z.record(z.string(), jsonValueSchema).nullable(),
  created_at: z.string(),
});

export const auditLogPageSchema = z.looseObject({
  entries: z.array(auditLogEntrySchema),
  next_before_id: z.number().nullable(),
});

export type PasskeyCapability = z.infer<typeof passkeyCapabilitySchema>;
export type PasskeyInfo = z.infer<typeof passkeyInfoSchema>;
export type AdminUser = z.infer<typeof adminUserSchema>;
export type OwnerIdentity = z.infer<typeof ownerIdentitySchema>;
export type AuditLogEntry = z.infer<typeof auditLogEntrySchema>;
export type AuditLogPage = z.infer<typeof auditLogPageSchema>;
