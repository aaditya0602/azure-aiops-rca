---
id: rb-cache-eviction-v1
service: cache
symptoms:
  - cache client-span latency elevated
  - inventory self time elevated while its downstream looks healthy
allowed_actions:
  - verb: scale
    target: cache
    required_preconditions:
      - eviction_rate_elevated
  - verb: restart
    target: cache
    required_preconditions:
      - cache_is_warm_replica_available
      - traffic_shifted_away
---

# Cache pressure and eviction storms

## Diagnose

1. Check eviction rate and memory headroom.
2. Confirm hit ratio drop is sustained rather than a single spike.

## Mitigate

Scale first. A restart empties the cache and moves the load onto the datastore
behind it, so it is only safe when a warm replica can serve reads and traffic has
already been shifted away.
