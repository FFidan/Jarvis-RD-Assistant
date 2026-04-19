export interface TimezoneOption {
  value: string;       // IANA zone id
  label: string;       // "(UTC+01:00) Amsterdam, Berlin, Vienna"
  region: string;
  searchTerms: string; // lowercase — zone id + offset + city names
}

export const TIMEZONE_OPTIONS: TimezoneOption[] = [
  // Africa
  { value: 'Africa/Cairo',        label: '(UTC+02:00) Cairo',                                    region: 'Africa',   searchTerms: 'africa/cairo utc+02:00 cairo egypt' },
  { value: 'Africa/Johannesburg', label: '(UTC+02:00) Johannesburg, Harare, Pretoria',           region: 'Africa',   searchTerms: 'africa/johannesburg utc+02:00 johannesburg harare pretoria' },
  { value: 'Africa/Lagos',        label: '(UTC+01:00) Lagos, Kinshasa, Brazzaville',             region: 'Africa',   searchTerms: 'africa/lagos utc+01:00 lagos kinshasa brazzaville' },
  { value: 'Africa/Nairobi',      label: '(UTC+03:00) Nairobi, Addis Ababa, Mogadishu',          region: 'Africa',   searchTerms: 'africa/nairobi utc+03:00 nairobi addis ababa mogadishu' },
  // America
  { value: 'America/Anchorage',   label: '(UTC-09:00) Anchorage',                               region: 'America',  searchTerms: 'america/anchorage utc-09:00 anchorage alaska' },
  { value: 'America/Argentina/Buenos_Aires', label: '(UTC-03:00) Buenos Aires, Georgetown',     region: 'America',  searchTerms: 'america/argentina utc-03:00 buenos aires georgetown' },
  { value: 'America/Bogota',      label: '(UTC-05:00) Bogotá, Lima, Quito',                     region: 'America',  searchTerms: 'america/bogota utc-05:00 bogota lima quito' },
  { value: 'America/Chicago',     label: '(UTC-06:00) Chicago, Dallas, Mexico City',            region: 'America',  searchTerms: 'america/chicago utc-06:00 chicago dallas mexico city central' },
  { value: 'America/Denver',      label: '(UTC-07:00) Denver, Phoenix, Salt Lake City',         region: 'America',  searchTerms: 'america/denver utc-07:00 denver phoenix mountain' },
  { value: 'America/Halifax',     label: '(UTC-04:00) Halifax, Atlantic Time',                  region: 'America',  searchTerms: 'america/halifax utc-04:00 halifax atlantic' },
  { value: 'America/Los_Angeles', label: '(UTC-08:00) Los Angeles, Seattle, Vancouver',         region: 'America',  searchTerms: 'america/los_angeles utc-08:00 los angeles seattle vancouver pacific' },
  { value: 'America/New_York',    label: '(UTC-05:00) New York, Toronto, Miami',                region: 'America',  searchTerms: 'america/new_york utc-05:00 new york toronto miami eastern' },
  { value: 'America/Sao_Paulo',   label: '(UTC-03:00) São Paulo, Brasília',                     region: 'America',  searchTerms: 'america/sao_paulo utc-03:00 sao paulo brasilia' },
  { value: 'America/Santiago',    label: '(UTC-04:00) Santiago',                                region: 'America',  searchTerms: 'america/santiago utc-04:00 santiago chile' },
  { value: 'America/St_Johns',    label: "(UTC-03:30) St. John's, Newfoundland",                region: 'America',  searchTerms: "america/st_johns utc-03:30 st johns newfoundland" },
  // Asia
  { value: 'Asia/Almaty',         label: '(UTC+06:00) Almaty, Dhaka',                           region: 'Asia',     searchTerms: 'asia/almaty utc+06:00 almaty dhaka' },
  { value: 'Asia/Baghdad',        label: '(UTC+03:00) Baghdad, Kuwait, Riyadh',                 region: 'Asia',     searchTerms: 'asia/baghdad utc+03:00 baghdad kuwait riyadh' },
  { value: 'Asia/Bangkok',        label: '(UTC+07:00) Bangkok, Hanoi, Jakarta',                 region: 'Asia',     searchTerms: 'asia/bangkok utc+07:00 bangkok hanoi jakarta' },
  { value: 'Asia/Dubai',          label: '(UTC+04:00) Dubai, Abu Dhabi, Muscat',                region: 'Asia',     searchTerms: 'asia/dubai utc+04:00 dubai abu dhabi muscat' },
  { value: 'Asia/Hong_Kong',      label: '(UTC+08:00) Hong Kong, Singapore, Beijing',           region: 'Asia',     searchTerms: 'asia/hong_kong utc+08:00 hong kong singapore beijing' },
  { value: 'Asia/Kabul',          label: '(UTC+04:30) Kabul',                                   region: 'Asia',     searchTerms: 'asia/kabul utc+04:30 kabul afghanistan' },
  { value: 'Asia/Karachi',        label: '(UTC+05:00) Karachi, Islamabad, Tashkent',            region: 'Asia',     searchTerms: 'asia/karachi utc+05:00 karachi islamabad tashkent' },
  { value: 'Asia/Kathmandu',      label: '(UTC+05:45) Kathmandu',                               region: 'Asia',     searchTerms: 'asia/kathmandu utc+05:45 kathmandu nepal' },
  { value: 'Asia/Kolkata',        label: '(UTC+05:30) Mumbai, New Delhi, Kolkata',              region: 'Asia',     searchTerms: 'asia/kolkata utc+05:30 mumbai new delhi kolkata india' },
  { value: 'Asia/Rangoon',        label: '(UTC+06:30) Yangon (Rangoon)',                        region: 'Asia',     searchTerms: 'asia/rangoon utc+06:30 yangon rangoon myanmar' },
  { value: 'Asia/Seoul',          label: '(UTC+09:00) Seoul, Tokyo, Osaka',                     region: 'Asia',     searchTerms: 'asia/seoul utc+09:00 seoul tokyo osaka' },
  { value: 'Asia/Shanghai',       label: '(UTC+08:00) Shanghai, Chongqing, Urumqi',             region: 'Asia',     searchTerms: 'asia/shanghai utc+08:00 shanghai chongqing urumqi' },
  { value: 'Asia/Tehran',         label: '(UTC+03:30) Tehran',                                  region: 'Asia',     searchTerms: 'asia/tehran utc+03:30 tehran iran' },
  { value: 'Asia/Vladivostok',    label: '(UTC+10:00) Vladivostok',                             region: 'Asia',     searchTerms: 'asia/vladivostok utc+10:00 vladivostok' },
  { value: 'Asia/Yekaterinburg',  label: '(UTC+05:00) Yekaterinburg',                           region: 'Asia',     searchTerms: 'asia/yekaterinburg utc+05:00 yekaterinburg' },
  // Atlantic
  { value: 'Atlantic/Azores',     label: '(UTC-01:00) Azores',                                  region: 'Atlantic', searchTerms: 'atlantic/azores utc-01:00 azores' },
  { value: 'Atlantic/Cape_Verde', label: '(UTC-01:00) Cape Verde Islands',                      region: 'Atlantic', searchTerms: 'atlantic/cape_verde utc-01:00 cape verde' },
  // Australia / Pacific
  { value: 'Australia/Adelaide',  label: '(UTC+09:30) Adelaide',                                region: 'Pacific',  searchTerms: 'australia/adelaide utc+09:30 adelaide' },
  { value: 'Australia/Brisbane',  label: '(UTC+10:00) Brisbane',                                region: 'Pacific',  searchTerms: 'australia/brisbane utc+10:00 brisbane' },
  { value: 'Australia/Darwin',    label: '(UTC+09:30) Darwin',                                  region: 'Pacific',  searchTerms: 'australia/darwin utc+09:30 darwin' },
  { value: 'Australia/Sydney',    label: '(UTC+11:00) Sydney, Melbourne, Hobart',               region: 'Pacific',  searchTerms: 'australia/sydney utc+11:00 sydney melbourne hobart' },
  { value: 'Pacific/Auckland',    label: '(UTC+13:00) Auckland, Wellington',                    region: 'Pacific',  searchTerms: 'pacific/auckland utc+13:00 auckland wellington' },
  { value: 'Pacific/Fiji',        label: '(UTC+12:00) Fiji, Marshall Islands',                  region: 'Pacific',  searchTerms: 'pacific/fiji utc+12:00 fiji' },
  { value: 'Pacific/Honolulu',    label: '(UTC-10:00) Hawaii',                                  region: 'Pacific',  searchTerms: 'pacific/honolulu utc-10:00 hawaii honolulu' },
  // Europe
  { value: 'Europe/Amsterdam',    label: '(UTC+01:00) Amsterdam, Berlin, Vienna, Rome',         region: 'Europe',   searchTerms: 'europe/amsterdam utc+01:00 amsterdam berlin vienna rome stockholm' },
  { value: 'Europe/Athens',       label: '(UTC+02:00) Athens, Bucharest, Helsinki',             region: 'Europe',   searchTerms: 'europe/athens utc+02:00 athens bucharest helsinki' },
  { value: 'Europe/Istanbul',     label: '(UTC+03:00) Istanbul, Ankara',                        region: 'Europe',   searchTerms: 'europe/istanbul utc+03:00 istanbul ankara turkey' },
  { value: 'Europe/London',       label: '(UTC+00:00) London, Dublin, Lisbon, Edinburgh',       region: 'Europe',   searchTerms: 'europe/london utc+00:00 london dublin lisbon edinburgh' },
  { value: 'Europe/Madrid',       label: '(UTC+01:00) Madrid, Paris, Brussels, Warsaw',         region: 'Europe',   searchTerms: 'europe/madrid utc+01:00 madrid paris brussels warsaw' },
  { value: 'Europe/Moscow',       label: '(UTC+03:00) Moscow, St. Petersburg, Volgograd',       region: 'Europe',   searchTerms: 'europe/moscow utc+03:00 moscow st petersburg volgograd' },
  // UTC
  { value: 'UTC',                 label: '(UTC+00:00) Coordinated Universal Time',              region: 'Other',    searchTerms: 'utc utc+00:00 coordinated universal time' },
];

export const TIMEZONE_BY_VALUE = new Map(TIMEZONE_OPTIONS.map(t => [t.value, t]));
export const TIMEZONE_REGIONS = ['Africa', 'America', 'Asia', 'Atlantic', 'Europe', 'Pacific', 'Other'] as const;
