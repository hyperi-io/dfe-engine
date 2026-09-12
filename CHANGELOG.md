# Changelog

Rendered by CI and committed back at the end of a release -- do not edit by
hand. Release notes also appear on the GitHub Releases page, one per tag.

## [1.20.1](https://github.com/hyperi-io/dfe-engine/compare/v1.20.0...v1.20.1) (2026-09-12)

### Bug Fixes

* **gitops:** give every replica one view of the deploy repo ([7575d13](https://github.com/hyperi-io/dfe-engine/commit/7575d13f251f12467db20ea992bb3d6eac653273)), closes [#361](https://github.com/hyperi-io/dfe-engine/issues/361)

## [1.20.0](https://github.com/hyperi-io/dfe-engine/compare/v1.19.37...v1.20.0) (2026-09-12)

### Features

* account changes ([#360](https://github.com/hyperi-io/dfe-engine/issues/360)) ([f50f732](https://github.com/hyperi-io/dfe-engine/commit/f50f7329190ea4189761e89acd409f85e9bf7e8a))

### Bug Fixes

* **appmgmt:** render each app's config where no chart does it ([#368](https://github.com/hyperi-io/dfe-engine/issues/368)) ([e7d6aba](https://github.com/hyperi-io/dfe-engine/commit/e7d6abaea967a15e668b6b0923cd6fe340ee3514))
* create a HyperDX source for each DFE source on deploy ([#364](https://github.com/hyperi-io/dfe-engine/issues/364)) ([96c7fae](https://github.com/hyperi-io/dfe-engine/commit/96c7fae3e9a0df969b66bea59db257e39d30a96d))
* drop the stale schemas gitlink so the release checkout survives ([8d26ad2](https://github.com/hyperi-io/dfe-engine/commit/8d26ad2cb90db69b58ccdebd16678f122868db56)), closes [#360](https://github.com/hyperi-io/dfe-engine/issues/360) [#367](https://github.com/hyperi-io/dfe-engine/issues/367)
* write the HyperDX source to every team, and read back where it landed ([#366](https://github.com/hyperi-io/dfe-engine/issues/366)) ([40f4e3e](https://github.com/hyperi-io/dfe-engine/commit/40f4e3e9cfeb45cc591858f77eecf89061fe9e5b))

## [1.19.37](https://github.com/hyperi-io/dfe-engine/compare/v1.19.36...v1.19.37) (2026-09-11)

### Bug Fixes

* seed a core source for the landing table ([#358](https://github.com/hyperi-io/dfe-engine/issues/358)) ([92d7dd1](https://github.com/hyperi-io/dfe-engine/commit/92d7dd149fc70093e98e19ba6a23f6a42e827604)), closes [#295](https://github.com/hyperi-io/dfe-engine/issues/295)
* take dfe-schemas 0.2.2 for sources/ ([#359](https://github.com/hyperi-io/dfe-engine/issues/359)) ([e0b316b](https://github.com/hyperi-io/dfe-engine/commit/e0b316bc5a47d9695ce221480c61b12cfc413db0))

## [1.19.36](https://github.com/hyperi-io/dfe-engine/compare/v1.19.35...v1.19.36) (2026-09-11)

### Bug Fixes

* **appmgmt:** refuse a source needing its own app where Compose cannot make one ([#356](https://github.com/hyperi-io/dfe-engine/issues/356)) ([ca635f7](https://github.com/hyperi-io/dfe-engine/commit/ca635f78a89f65fb15837994b96241f1fc7be523)), closes [dfe-docker#99](https://github.com/hyperi-io/dfe-docker/issues/99)
* **deps:** update dependency node to v24.20.0 ([#296](https://github.com/hyperi-io/dfe-engine/issues/296)) ([0c67bc7](https://github.com/hyperi-io/dfe-engine/commit/0c67bc764bf6ee70c30b6b9e7b4d1053a2c23550))

## [1.19.35](https://github.com/hyperi-io/dfe-engine/compare/v1.19.34...v1.19.35) (2026-09-10)

### Bug Fixes

* **docs:** the fetcher's ingest listener defaults off, and a Compose deployment applies no routing ([808e3b4](https://github.com/hyperi-io/dfe-engine/commit/808e3b4d55570fba6a3a7d70f14bb4b41059fa2f)), closes [#85](https://github.com/hyperi-io/dfe-engine/issues/85)

## [1.19.34](https://github.com/hyperi-io/dfe-engine/compare/v1.19.33...v1.19.34) (2026-09-10)

### Bug Fixes

* skip the routed flow shapes where nothing applies the routing ([9a4cff6](https://github.com/hyperi-io/dfe-engine/commit/9a4cff65626fff390536a79920dac2b69d3ed560))

## [1.19.33](https://github.com/hyperi-io/dfe-engine/compare/v1.19.32...v1.19.33) (2026-09-10)

### Bug Fixes

* a posted batch lands one row per element, and a fetched source is refused where nothing fetches ([66bf4b9](https://github.com/hyperi-io/dfe-engine/commit/66bf4b997e9fd0d88b0d8370287eb4a374d76f28)), closes [#72](https://github.com/hyperi-io/dfe-engine/issues/72)

## [1.19.32](https://github.com/hyperi-io/dfe-engine/compare/v1.19.31...v1.19.32) (2026-09-10)

### Bug Fixes

* **hunt-runner:** a fire stays owed until its watermark is written ([065691f](https://github.com/hyperi-io/dfe-engine/commit/065691f89b0094c0ad8a16f32e194281aacc9980)), closes [#350](https://github.com/hyperi-io/dfe-engine/issues/350)

## [1.19.31](https://github.com/hyperi-io/dfe-engine/compare/v1.19.30...v1.19.31) (2026-09-10)

### Bug Fixes

* refuse a direct source on a bus-bound deployment ([8dad447](https://github.com/hyperi-io/dfe-engine/commit/8dad4476b742912792879b3718d94237ad96ffe4)), closes [#341](https://github.com/hyperi-io/dfe-engine/issues/341)

## [1.19.30](https://github.com/hyperi-io/dfe-engine/compare/v1.19.29...v1.19.30) (2026-09-10)

### Bug Fixes

* **gitops:** a replica's push never overwrites another's ([e81f8bf](https://github.com/hyperi-io/dfe-engine/commit/e81f8bf7c4bc0ed9f083e0632bf9a63948a57310))

## [1.19.29](https://github.com/hyperi-io/dfe-engine/compare/v1.19.28...v1.19.29) (2026-09-10)

### Bug Fixes

* rename the scale-mesh profile to mesh ([#345](https://github.com/hyperi-io/dfe-engine/issues/345)) ([f8a54c5](https://github.com/hyperi-io/dfe-engine/commit/f8a54c5ebcd6b7647e81c4be6d5f916c5fda3b52)), closes [#265](https://github.com/hyperi-io/dfe-engine/issues/265)

## [1.19.28](https://github.com/hyperi-io/dfe-engine/compare/v1.19.27...v1.19.28) (2026-09-10)

### Bug Fixes

* **e2e:** drop a flow table on every replica of the cluster ([#342](https://github.com/hyperi-io/dfe-engine/issues/342)) ([9e9bad1](https://github.com/hyperi-io/dfe-engine/commit/9e9bad17af582e953815dd6fd055a09547b0b851))
* **gitops:** fast-forward the clone to the remote head before every publish ([#344](https://github.com/hyperi-io/dfe-engine/issues/344)) ([20852ae](https://github.com/hyperi-io/dfe-engine/commit/20852ae62d429d9bfc35305020ccbe28a0afed0e))

## [1.19.27](https://github.com/hyperi-io/dfe-engine/compare/v1.19.26...v1.19.27) (2026-09-10)

### Bug Fixes

* **e2e:** retry an ingest the receiver answers 5xx mid-roll ([#339](https://github.com/hyperi-io/dfe-engine/issues/339)) ([3e34c2a](https://github.com/hyperi-io/dfe-engine/commit/3e34c2a7a18f5aae361bd269f02843350df2af8d))
* seed a deployment's default app instances when the deploy repo carries none ([#340](https://github.com/hyperi-io/dfe-engine/issues/340)) ([9352c4a](https://github.com/hyperi-io/dfe-engine/commit/9352c4a610e806165e4fda9b7f3424a36cdc1c7a))

## [1.19.26](https://github.com/hyperi-io/dfe-engine/compare/v1.19.25...v1.19.26) (2026-09-10)

### Bug Fixes

* seed the library from the content the deploy mounts ([#337](https://github.com/hyperi-io/dfe-engine/issues/337)) ([fc85982](https://github.com/hyperi-io/dfe-engine/commit/fc859827a116d5aa61b840f2bd41b7e3847aeefd)), closes [#263](https://github.com/hyperi-io/dfe-engine/issues/263)

## [1.19.25](https://github.com/hyperi-io/dfe-engine/compare/v1.19.24...v1.19.25) (2026-09-10)

### Bug Fixes

* report what the deployment is, and what a source is doing ([#333](https://github.com/hyperi-io/dfe-engine/issues/333)) ([a84dd3a](https://github.com/hyperi-io/dfe-engine/commit/a84dd3a0e77ce00b84f874875e9d4631fb426cd8))

## [1.19.24](https://github.com/hyperi-io/dfe-engine/compare/v1.19.23...v1.19.24) (2026-09-09)

### Bug Fixes

* sense the cluster when a source deploy builds its DDL ([#336](https://github.com/hyperi-io/dfe-engine/issues/336)) ([9963f85](https://github.com/hyperi-io/dfe-engine/commit/9963f856177c63c1ab53ab54860ec6049b21db59))

## [1.19.23](https://github.com/hyperi-io/dfe-engine/compare/v1.19.22...v1.19.23) (2026-09-09)

### Bug Fixes

* correct what the engine says about matching, the loader port and the catalogue ([#334](https://github.com/hyperi-io/dfe-engine/issues/334)) ([69bb5cf](https://github.com/hyperi-io/dfe-engine/commit/69bb5cf965277b32f3c5e18d21285016601ea511)), closes [#335](https://github.com/hyperi-io/dfe-engine/issues/335)
* **e2e:** run every flow shape the direct transport carries ([10f6852](https://github.com/hyperi-io/dfe-engine/commit/10f6852e3433a11a6d16790f49b2e5614f7367f1))

## [1.19.22](https://github.com/hyperi-io/dfe-engine/compare/v1.19.21...v1.19.22) (2026-09-09)

### Bug Fixes

* **appmgmt:** the engine compiles the apps' routing at startup ([6bbeef6](https://github.com/hyperi-io/dfe-engine/commit/6bbeef6cc7c984191503c214c57a9826631e627c))

## [1.19.21](https://github.com/hyperi-io/dfe-engine/compare/v1.19.20...v1.19.21) (2026-09-09)

### Bug Fixes

* **auth:** retire the bootstrap admin once a real admin exists ([02903c0](https://github.com/hyperi-io/dfe-engine/commit/02903c001da5bdea86040a242a71f39e8bb434e0))

## [1.19.20](https://github.com/hyperi-io/dfe-engine/compare/v1.19.19...v1.19.20) (2026-09-09)

### Bug Fixes

* the catch-all source and landing table are main everywhere ([351baaa](https://github.com/hyperi-io/dfe-engine/commit/351baaa33a9c573135c304945cd62fd8d8851b21))

## [1.19.19](https://github.com/hyperi-io/dfe-engine/compare/v1.19.18...v1.19.19) (2026-09-09)

### Bug Fixes

* a compiled gRPC destination is an object with an endpoint, and the flows suite runs both transports ([2fb89db](https://github.com/hyperi-io/dfe-engine/commit/2fb89db18ff0be4d023a2cbbabc321bc08cd7fc7))

## [1.19.18](https://github.com/hyperi-io/dfe-engine/compare/v1.19.17...v1.19.18) (2026-09-09)

### Bug Fixes

* **setup:** break-glass is not the first user ([41286f2](https://github.com/hyperi-io/dfe-engine/commit/41286f2624195eac8ffc142d11194a5bab77b25f))
* **setup:** the wizard has no password-reset step ([37f4047](https://github.com/hyperi-io/dfe-engine/commit/37f4047a2710cf52e1a724232c49ba34c19dc4e0))
* the manifest carries the composition axes and the archiver takes either transport ([105344d](https://github.com/hyperi-io/dfe-engine/commit/105344d4f4a7a05cd14de726619452e00a88e254))

## [1.19.17](https://github.com/hyperi-io/dfe-engine/compare/v1.19.16...v1.19.17) (2026-09-09)

### Bug Fixes

* the manifest says where an app is offered and deployed, and a source answers its flow ([84ebb3e](https://github.com/hyperi-io/dfe-engine/commit/84ebb3e850f88ffb0223eed08e23424d6d989d17))

## [1.19.16](https://github.com/hyperi-io/dfe-engine/compare/v1.19.15...v1.19.16) (2026-09-09)

### Bug Fixes

* the source catalogue, offered from the transform that ships it ([1f0a38b](https://github.com/hyperi-io/dfe-engine/commit/1f0a38b0f7096ed9b4270c138c8e04b8bc185bfe))

## [1.19.15](https://github.com/hyperi-io/dfe-engine/compare/v1.19.14...v1.19.15) (2026-09-09)

### Bug Fixes

* mesh listener addressing, and vrl and vector carry direct ([3b79346](https://github.com/hyperi-io/dfe-engine/commit/3b793469ea9121adf0416b1873acf0ff9bf1e9a6))

## [1.19.14](https://github.com/hyperi-io/dfe-engine/compare/v1.19.13...v1.19.14) (2026-09-09)

### Bug Fixes

* **appmgmt:** compilers emit per transport; named routing blocks; manifest endpoints ([2866f5e](https://github.com/hyperi-io/dfe-engine/commit/2866f5ee2f256e0befc0f6d7e5c67583b558086c))

## [1.19.13](https://github.com/hyperi-io/dfe-engine/compare/v1.19.12...v1.19.13) (2026-09-09)

### Bug Fixes

* **api:** system version reports the stack from the deploy repo pins ([d03629a](https://github.com/hyperi-io/dfe-engine/commit/d03629acc0e8f897acb4aba2a3a5a253a2196130))
* **source:** a source declares its transport, transform variant, archive and fetcher routes ([436dbdc](https://github.com/hyperi-io/dfe-engine/commit/436dbdc2315349dc89a82abb83f6a31691a00127)), closes [#315](https://github.com/hyperi-io/dfe-engine/issues/315)

## [1.19.12](https://github.com/hyperi-io/dfe-engine/compare/v1.19.11...v1.19.12) (2026-09-08)

### Bug Fixes

* **sources:** a source is receiver-based or fetcher-based, and a deploy delivers its routing ([92fb103](https://github.com/hyperi-io/dfe-engine/commit/92fb103c6d96fb3522cf7b4e2c83333632a8f9c7)), closes [#244](https://github.com/hyperi-io/dfe-engine/issues/244)

## [1.19.11](https://github.com/hyperi-io/dfe-engine/compare/v1.19.10...v1.19.11) (2026-09-08)

### Bug Fixes

* **deps:** dfe-schemas 0.2.1, the time-series tables take the deployment retention ([d3bc46f](https://github.com/hyperi-io/dfe-engine/commit/d3bc46f0b56ed9cbd6838cd7be3d451c3ea6d677))

## [1.19.10](https://github.com/hyperi-io/dfe-engine/compare/v1.19.9...v1.19.10) (2026-09-08)

### Bug Fixes

* **schema:** deployment default ttl for every time-series table ([8a82383](https://github.com/hyperi-io/dfe-engine/commit/8a82383b3fbed96669b3fda0e382457dec8c54f3)), closes [#308](https://github.com/hyperi-io/dfe-engine/issues/308)

## [1.19.9](https://github.com/hyperi-io/dfe-engine/compare/v1.19.8...v1.19.9) (2026-09-08)

### Bug Fixes

* **auth:** guard the spec version, the admin rotation and the changeme predicate ([45b1c33](https://github.com/hyperi-io/dfe-engine/commit/45b1c33002c76cb85fd635b70548b636f10fd517)), closes [#301](https://github.com/hyperi-io/dfe-engine/issues/301)
* **oidc:** hand the browser back to the console after an idp login ([6d87957](https://github.com/hyperi-io/dfe-engine/commit/6d87957885e62d8af8e5c1416f4457c8cc39de16)), closes [#245](https://github.com/hyperi-io/dfe-engine/issues/245)
* rename the landing table default to main ([#303](https://github.com/hyperi-io/dfe-engine/issues/303)) ([4f7ac4c](https://github.com/hyperi-io/dfe-engine/commit/4f7ac4c9cdbaf85fa879b3fef99bd2e5f5ef2e5e))
* resolve the oidc fixture login from env in the e2e conftest ([824ed28](https://github.com/hyperi-io/dfe-engine/commit/824ed287a92daf44c506498b4bd24761c5267758))

## [1.19.8](https://github.com/hyperi-io/dfe-engine/compare/v1.19.7...v1.19.8) (2026-09-07)

### Bug Fixes

* **auth:** seed admin and break-glass from the store and refuse changeme outside dev ([462d175](https://github.com/hyperi-io/dfe-engine/commit/462d175288d0a937accf0d2de9eab4761b61d053)), closes [#233](https://github.com/hyperi-io/dfe-engine/issues/233) [#77](https://github.com/hyperi-io/dfe-engine/issues/77) [#206](https://github.com/hyperi-io/dfe-engine/issues/206)
* carry the landing table setting through to the DDL ([#299](https://github.com/hyperi-io/dfe-engine/issues/299)) ([2d5c595](https://github.com/hyperi-io/dfe-engine/commit/2d5c5953e99e604749c2d597f5b4fb048e587da5)), closes [#297](https://github.com/hyperi-io/dfe-engine/issues/297)

## [1.19.7](https://github.com/hyperi-io/dfe-engine/compare/v1.19.6...v1.19.7) (2026-09-05)

### Bug Fixes

* **sigma:** propagation and alert links report review-required instead of claiming success ([#293](https://github.com/hyperi-io/dfe-engine/issues/293)) ([bac1cdd](https://github.com/hyperi-io/dfe-engine/commit/bac1cddad81c3eac12d82ce8284be162c0ffe59e)), closes [#287](https://github.com/hyperi-io/dfe-engine/issues/287)

## [1.19.6](https://github.com/hyperi-io/dfe-engine/compare/v1.19.5...v1.19.6) (2026-09-05)

### Bug Fixes

* **api:** trust the configured proxies for forwarded proto, and report the real auth mode ([#291](https://github.com/hyperi-io/dfe-engine/issues/291)) ([2a24070](https://github.com/hyperi-io/dfe-engine/commit/2a24070df6be1a5a304c326a93459a0430665832)), closes [#289](https://github.com/hyperi-io/dfe-engine/issues/289) [#290](https://github.com/hyperi-io/dfe-engine/issues/290)

## [1.19.5](https://github.com/hyperi-io/dfe-engine/compare/v1.19.4...v1.19.5) (2026-09-04)

### Bug Fixes

* **gitops:** scrub the clone's remote credentials, surface hunt write outcomes, and finish the configured admin name ([#287](https://github.com/hyperi-io/dfe-engine/issues/287)) ([5203193](https://github.com/hyperi-io/dfe-engine/commit/520319364dc92ecf5ec548455d0c339ce5de59d4)), closes [282-#286](https://github.com/hyperi-io/282-/issues/286)

## [1.19.4](https://github.com/hyperi-io/dfe-engine/compare/v1.19.3...v1.19.4) (2026-09-04)

### Bug Fixes

* **setup:** measure break-glass rotation against the configured bootstrap password ([#286](https://github.com/hyperi-io/dfe-engine/issues/286)) ([7db7db0](https://github.com/hyperi-io/dfe-engine/commit/7db7db0cae23bcf91a11b1845f2f7329a993633f)), closes [#284](https://github.com/hyperi-io/dfe-engine/issues/284) [#235](https://github.com/hyperi-io/dfe-engine/issues/235)

## [1.19.3](https://github.com/hyperi-io/dfe-engine/compare/v1.19.2...v1.19.3) (2026-09-04)

### Bug Fixes

* **hunts:** run-now writes through the API's ClickHouse client, and gitops pushes stop logging the token ([#285](https://github.com/hyperi-io/dfe-engine/issues/285)) ([7889af7](https://github.com/hyperi-io/dfe-engine/commit/7889af72a528f39d39928eae285f0c4d7ef3e2ca))

## [1.19.2](https://github.com/hyperi-io/dfe-engine/compare/v1.19.1...v1.19.2) (2026-09-04)

### Bug Fixes

* **auth:** read the break-glass admin name and password from the environment ([#284](https://github.com/hyperi-io/dfe-engine/issues/284)) ([d3d9a1f](https://github.com/hyperi-io/dfe-engine/commit/d3d9a1fccf08a16f62523647ca256cc2fa9ddfe5))
* **hunts:** hunts status reports runner liveness from a heartbeat ([#283](https://github.com/hyperi-io/dfe-engine/issues/283)) ([3788592](https://github.com/hyperi-io/dfe-engine/commit/3788592cafb9ba39e9e31e90aa61b2ff8022c76e)), closes [#284](https://github.com/hyperi-io/dfe-engine/issues/284)

## [1.19.1](https://github.com/hyperi-io/dfe-engine/compare/v1.19.0...v1.19.1) (2026-09-04)

### Bug Fixes

* **hunts:** commit hunts and rules to the deploy repo when gitops is on ([#282](https://github.com/hyperi-io/dfe-engine/issues/282)) ([cf99c67](https://github.com/hyperi-io/dfe-engine/commit/cf99c67aff060b0fe74f6b4ebde7814585189582)), closes [dfe-infra#213](https://github.com/hyperi-io/dfe-infra/issues/213) [#281](https://github.com/hyperi-io/dfe-engine/issues/281) [hyperi-io/dfe-infra#212](https://github.com/hyperi-io/dfe-infra/issues/212)
* **tests:** stub the segment in the standing-stream tests so they stop killing xdist workers ([#281](https://github.com/hyperi-io/dfe-engine/issues/281)) ([a52727e](https://github.com/hyperi-io/dfe-engine/commit/a52727e9aba2caeb3a6dc91811d3d9ab0391a03b))

## [1.19.0](https://github.com/hyperi-io/dfe-engine/compare/v1.18.0...v1.19.0) (2026-09-04)

### Features

* **hunts:** per-hunt run visibility and a working run-now ([#279](https://github.com/hyperi-io/dfe-engine/issues/279)) ([97da403](https://github.com/hyperi-io/dfe-engine/commit/97da403b30ea3239bc98b57d3fe84839959fbe0f)), closes [#242](https://github.com/hyperi-io/dfe-engine/issues/242) [#237](https://github.com/hyperi-io/dfe-engine/issues/237) [#238](https://github.com/hyperi-io/dfe-engine/issues/238) [#280](https://github.com/hyperi-io/dfe-engine/issues/280) [#273](https://github.com/hyperi-io/dfe-engine/issues/273)

### Bug Fixes

* **kafka:** create a source's topics instead of relying on broker auto-create ([#238](https://github.com/hyperi-io/dfe-engine/issues/238)) ([2a80b0a](https://github.com/hyperi-io/dfe-engine/commit/2a80b0a94e790784089be20e1821a31afc1787ec)), closes [#97](https://github.com/hyperi-io/dfe-engine/issues/97) [#97](https://github.com/hyperi-io/dfe-engine/issues/97)
* make the chart dials reachable and stop emitting defaults the apps reject ([#242](https://github.com/hyperi-io/dfe-engine/issues/242)) ([973015d](https://github.com/hyperi-io/dfe-engine/commit/973015d850104b963db5831bda19577e2222c0a1)), closes [#116](https://github.com/hyperi-io/dfe-engine/issues/116)
* **source:** name the common-header profile that exists, and make the filebeat E2E honest ([#237](https://github.com/hyperi-io/dfe-engine/issues/237)) ([1aaaf8e](https://github.com/hyperi-io/dfe-engine/commit/1aaaf8ebc1fc38f3ca3bd31aaa6bf8e3416aa587)), closes [dfe-loader#127](https://github.com/hyperi-io/dfe-loader/issues/127) [dfe-loader#127](https://github.com/hyperi-io/dfe-loader/issues/127) [dfe-loader#127](https://github.com/hyperi-io/dfe-loader/issues/127)
* **tests:** find the checkout's .env from a worktree, and say when ClickHouse falls back to local docker ([#280](https://github.com/hyperi-io/dfe-engine/issues/280)) ([e9ba253](https://github.com/hyperi-io/dfe-engine/commit/e9ba253d78c569e9da473e30b54e23ef2a8acd13))

## [1.18.0](https://github.com/hyperi-io/dfe-engine/compare/v1.17.13...v1.18.0) (2026-09-04)

### Features

* add endpoint for delete schema version ([#243](https://github.com/hyperi-io/dfe-engine/issues/243)) ([ce578f5](https://github.com/hyperi-io/dfe-engine/commit/ce578f5c1014d98b579f3f1a45cac6dc497cf0e3))

### Bug Fixes

* **auth:** read X-Forwarded-For only behind a trusted proxy ([#268](https://github.com/hyperi-io/dfe-engine/issues/268)) ([1cb8055](https://github.com/hyperi-io/dfe-engine/commit/1cb8055c0115dd7667c02de471ecac3caf37fab3)), closes [#263](https://github.com/hyperi-io/dfe-engine/issues/263)
* **auth:** write auth.login.denied where the credential is refused ([#267](https://github.com/hyperi-io/dfe-engine/issues/267)) ([d40e908](https://github.com/hyperi-io/dfe-engine/commit/d40e90834a37e71b913efe4148e2a11169c9d2d8)), closes [#274](https://github.com/hyperi-io/dfe-engine/issues/274) [#260](https://github.com/hyperi-io/dfe-engine/issues/260) [#264](https://github.com/hyperi-io/dfe-engine/issues/264) [#259](https://github.com/hyperi-io/dfe-engine/issues/259) [#266](https://github.com/hyperi-io/dfe-engine/issues/266) [#258](https://github.com/hyperi-io/dfe-engine/issues/258) [#268](https://github.com/hyperi-io/dfe-engine/issues/268) [#269](https://github.com/hyperi-io/dfe-engine/issues/269) [#261](https://github.com/hyperi-io/dfe-engine/issues/261)
* **auth:** write auth.login.success on a credential exchange, not on every request ([#258](https://github.com/hyperi-io/dfe-engine/issues/258)) ([db29299](https://github.com/hyperi-io/dfe-engine/commit/db2929996320b3fd1e5b35bdec5874705dbd7151))
* **chart:** let the code's TLS default govern instead of disabling verification ([#251](https://github.com/hyperi-io/dfe-engine/issues/251)) ([645d631](https://github.com/hyperi-io/dfe-engine/commit/645d631e87e78ba786d69a2afa9baccab29e3308))
* **docker:** refresh the runtime base digest to the current python:3.12-slim ([#247](https://github.com/hyperi-io/dfe-engine/issues/247)) ([c28647f](https://github.com/hyperi-io/dfe-engine/commit/c28647f64d4bbd81e7b57e60af7d0080c671d950)), closes [#36](https://github.com/hyperi-io/dfe-engine/issues/36)
* drop the stale schemas gitlink left behind when the submodule went ([#278](https://github.com/hyperi-io/dfe-engine/issues/278)) ([6ad8bc2](https://github.com/hyperi-io/dfe-engine/commit/6ad8bc2d301cceac5a24e80966b9d58a48d7a569)), closes [#267](https://github.com/hyperi-io/dfe-engine/issues/267)
* **e2e:** probe the typed source table the way its schema allows ([dfcf75e](https://github.com/hyperi-io/dfe-engine/commit/dfcf75e9c7fe3ad0c5864a83ca40b02b0ffed748))
* **hunts:** compile a hunt's rules into the query the runner runs ([#276](https://github.com/hyperi-io/dfe-engine/issues/276)) ([6b204ab](https://github.com/hyperi-io/dfe-engine/commit/6b204abb7acb6414a0e295ed3a596dec2948c94c)), closes [#272](https://github.com/hyperi-io/dfe-engine/issues/272)
* **hunts:** stop requiring global_target_table_name, which nothing consumes ([#260](https://github.com/hyperi-io/dfe-engine/issues/260)) ([fd58593](https://github.com/hyperi-io/dfe-engine/commit/fd58593edbba523806e95df576697d790d1c4278))
* **jit:** log a refused or failed HyperDX invite instead of swallowing it ([#275](https://github.com/hyperi-io/dfe-engine/issues/275)) ([0be75cb](https://github.com/hyperi-io/dfe-engine/commit/0be75cbd38d62c2f754b2cbeada2b040026b2baa))
* **jit:** schedule the HyperDX invite on the running loop, or say it was skipped ([#269](https://github.com/hyperi-io/dfe-engine/issues/269)) ([cac3c14](https://github.com/hyperi-io/dfe-engine/commit/cac3c145e1206a461b2f6d27a61a90002c72af2e)), closes [#262](https://github.com/hyperi-io/dfe-engine/issues/262)
* **rules:** parse rule SQL as ClickHouse instead of grepping for keywords ([#277](https://github.com/hyperi-io/dfe-engine/issues/277)) ([01112db](https://github.com/hyperi-io/dfe-engine/commit/01112db5dc4b5099c03150cd4241cfcd4018524c))
* **services:** drop the auto_init block dfe-loader has no field for ([5dfe443](https://github.com/hyperi-io/dfe-engine/commit/5dfe443e02004022b7db73de2f98ac6ace9a8524))
* setup flow remove admin env set ([#250](https://github.com/hyperi-io/dfe-engine/issues/250)) ([92d143a](https://github.com/hyperi-io/dfe-engine/commit/92d143a230fd4350ce9981804ea7c584a38ce237))
* **source:** accept every catalogued transform engine instead of a literal vector/wasm list ([#264](https://github.com/hyperi-io/dfe-engine/issues/264)) ([56f4a30](https://github.com/hyperi-io/dfe-engine/commit/56f4a30e29a55f175ea3bc6351914d218df3d098))

## [1.17.13](https://github.com/hyperi-io/dfe-engine/compare/v1.17.12...v1.17.13) (2026-08-31)

### Bug Fixes

* **e2e:** probe the filebeat table with queries that can actually match ([d19b47a](https://github.com/hyperi-io/dfe-engine/commit/d19b47ac2e4d2ac04894dd4345eb27dd654d90ae))
* **e2e:** wrap the corpus with tags as a list, not a map ([3cf9e71](https://github.com/hyperi-io/dfe-engine/commit/3cf9e712bf9153263846e8ecb8bddbe829ae1ca9))
* **gitcrud:** build a commit subject that fits, instead of raising ([e5d4fe9](https://github.com/hyperi-io/dfe-engine/commit/e5d4fe9088940ab7121811bcaa238f02dc626e3d))
* gitignore the agent working dirs and the kubectl cache ([4cbbbb9](https://github.com/hyperi-io/dfe-engine/commit/4cbbbb9bd8e03a36cdc5cf0cb82d40259f746826))
* **governance:** one storage vocabulary, and a reload verdict that knows the operator ([ec2bf58](https://github.com/hyperi-io/dfe-engine/commit/ec2bf58a284011a627a6284c8229d938c2fe10bd))
* **loader:** emit the routing contract dfe-loader actually implements ([46974c5](https://github.com/hyperi-io/dfe-engine/commit/46974c58533c3bb60e307e44373a89918ad4e8bf))
* **schema:** consume dfe-schemas as a version-pinned wheel, drop the submodule ([c67f2c9](https://github.com/hyperi-io/dfe-engine/commit/c67f2c9c4ec90031a9dbfa1ae1d2b8ac0211268c))
* the app surface hands out the revision an If-Match write needs ([8ca2e91](https://github.com/hyperi-io/dfe-engine/commit/8ca2e9141ba02c21c3e4f5692114367654daa641))
* **yaml:** never fold a scalar, so stored file bodies survive a rewrite ([077a49b](https://github.com/hyperi-io/dfe-engine/commit/077a49b9a008a641838b396a4f6e362573f9c55e))

## [1.17.12](https://github.com/hyperi-io/dfe-engine/compare/v1.17.11...v1.17.12) (2026-08-30)

### Bug Fixes

* a versioned artefact library, stored as real files in git ([26f8fa9](https://github.com/hyperi-io/dfe-engine/commit/26f8fa98066bd092483b65877992182a6c6436bd))
* e2e seeds for the source, library and components UI areas ([5227598](https://github.com/hyperi-io/dfe-engine/commit/52275983be9e6751737c0ec047a57e1c8c826267))
* enrichment tables are a file set like any other ([c1d85dd](https://github.com/hyperi-io/dfe-engine/commit/c1d85dd1baf7637c47828ea4e13fbf054abb6faa))
* generic app-management layer over the deploy overlay ([8162910](https://github.com/hyperi-io/dfe-engine/commit/816291056f3d7d9555815f17b3f5360443300b1a))
* **governance:** lock the clickhouse tiered dials like every other storage choice ([22ee44f](https://github.com/hyperi-io/dfe-engine/commit/22ee44ff4322841434a6ed557dfcd47627b24a98))
* guard the backing-service dials, and let a KEDA-off app set its count ([46927a4](https://github.com/hyperi-io/dfe-engine/commit/46927a4386dfb535b4e4a0f7336d32b296c74af0))
* infra_admin can run the dry run it is meant to author ([feff12b](https://github.com/hyperi-io/dfe-engine/commit/feff12bf6bddbf4d04edf928d5961c78f944b8fb))
* quote the grouping query's table reference like every other sink ([7df4d0f](https://github.com/hyperi-io/dfe-engine/commit/7df4d0fbef3b0707b1d0536808fac580816f70d2))
* quote the identifiers the DDL generator emits ([6ee723d](https://github.com/hyperi-io/dfe-engine/commit/6ee723d22321b4e71e40d2803794d71509710c11))
* reflect the app catalogue, and remediate the review findings ([01cbf4d](https://github.com/hyperi-io/dfe-engine/commit/01cbf4df9d212fa7fd8b5df1e11b3e489295fd31))
* refuse a post-deploy storage-model change, and show what was declared ([13d4d6e](https://github.com/hyperi-io/dfe-engine/commit/13d4d6ecff081e2d788bf2dfcf1e566fe37b07bb))
* reset_all clears the substrate overlay it was leaving behind ([af0a71e](https://github.com/hyperi-io/dfe-engine/commit/af0a71edf2c6091f4e9c03cfd089f81fca781146))
* run a transform over real events before committing it ([ba11431](https://github.com/hyperi-io/dfe-engine/commit/ba114316211341039148b34bb082cdf0224e3ce1))
* seeding the break-glass admin persists it, so setup can finish ([f42b3df](https://github.com/hyperi-io/dfe-engine/commit/f42b3df12e122ed166f9b5ec0158c0ce8d3f42a4))
* source names are DNS-1123 labels, and the source guard fires ([f461630](https://github.com/hyperi-io/dfe-engine/commit/f46163036ba506de63348e5874d1fdd008185f6b))
* source routing now reaches the app that has to obey it ([c6668e6](https://github.com/hyperi-io/dfe-engine/commit/c6668e6c0f79559d46da943be989f34d53db9672))
* the duplicate-source workflow test uses a legal name ([836bd74](https://github.com/hyperi-io/dfe-engine/commit/836bd740aba9d876be615f6b537aefedac857475))
* validate the Vector shape the app actually loads ([e864b09](https://github.com/hyperi-io/dfe-engine/commit/e864b09afafe6dd1f1ffed1d1650a60abc3a682b))

## [1.17.11](https://github.com/hyperi-io/dfe-engine/compare/v1.17.10...v1.17.11) (2026-08-27)

### Bug Fixes

* version check on by default via the releases endpoint ([0d00257](https://github.com/hyperi-io/dfe-engine/commit/0d00257b59792f97c01be43cd80d5d76cc6b192a))

## [1.17.10](https://github.com/hyperi-io/dfe-engine/compare/v1.17.9...v1.17.10) (2026-08-27)

### Bug Fixes

* scalo 2.29.18 + startup version check, schemas submodule to retention head ([4f33bc9](https://github.com/hyperi-io/dfe-engine/commit/4f33bc9ec35bebf68cb790a549b6182d9fe3de62))

## [1.17.9](https://github.com/hyperi-io/dfe-engine/compare/v1.17.8...v1.17.9) (2026-08-27)

### Bug Fixes

* **governance:** tenant fence on by default, retention aligned to the partition ([#219](https://github.com/hyperi-io/dfe-engine/issues/219)) ([ed6bb1a](https://github.com/hyperi-io/dfe-engine/commit/ed6bb1abf4cb5febc7efcc13df1fce6e9ae1e952))
* **rules:** build a hyperdx rule from the view's SQL, not a caller's string ([#218](https://github.com/hyperi-io/dfe-engine/issues/218)) ([fe401f1](https://github.com/hyperi-io/dfe-engine/commit/fe401f1b507659abe0edfbdd5391fa51e6a3ed42)), closes [dfe-hyperdx#49](https://github.com/hyperi-io/dfe-hyperdx/issues/49)

## [1.17.8](https://github.com/hyperi-io/dfe-engine/compare/v1.17.7...v1.17.8) (2026-08-26)

### Bug Fixes

* **bootstrap:** re-seed schemas when the marker names another engine version ([#217](https://github.com/hyperi-io/dfe-engine/issues/217)) ([3fdd4ef](https://github.com/hyperi-io/dfe-engine/commit/3fdd4efb147079ff22654952ec73fb35bbe90c52))

## [1.17.7](https://github.com/hyperi-io/dfe-engine/compare/v1.17.6...v1.17.7) (2026-08-26)

### Bug Fixes

* **schema:** read the otel and engine-state tables from dfe-schemas ([#214](https://github.com/hyperi-io/dfe-engine/issues/214)) ([5adc138](https://github.com/hyperi-io/dfe-engine/commit/5adc138122f0616235ef9d8e35294c207f64dacb))

## [1.17.6](https://github.com/hyperi-io/dfe-engine/compare/v1.17.5...v1.17.6) (2026-08-26)

### Bug Fixes

* **schema:** stop the query-log archive loading the API's settings too ([#213](https://github.com/hyperi-io/dfe-engine/issues/213)) ([667b329](https://github.com/hyperi-io/dfe-engine/commit/667b3290785f730851dd68697a765c47f5fec3d0))

## [1.17.5](https://github.com/hyperi-io/dfe-engine/compare/v1.17.4...v1.17.5) (2026-08-25)

### Bug Fixes

* **schema:** find the schemas the image ships, from any process in it ([#212](https://github.com/hyperi-io/dfe-engine/issues/212)) ([7c1317d](https://github.com/hyperi-io/dfe-engine/commit/7c1317d31f28bc29d04401cbd4efcc5a6a9fcf42))

## [1.17.4](https://github.com/hyperi-io/dfe-engine/compare/v1.17.3...v1.17.4) (2026-08-25)

### Bug Fixes

* **schema:** stop dfe-schema loading the API's settings ([#211](https://github.com/hyperi-io/dfe-engine/issues/211)) ([5246e0a](https://github.com/hyperi-io/dfe-engine/commit/5246e0a7e288289de636cd5feed6533bce769463))

## [1.17.3](https://github.com/hyperi-io/dfe-engine/compare/v1.17.2...v1.17.3) (2026-08-25)

### Bug Fixes

* **schema:** one DDL writer, one database, and a dfe-schema entry point ([#210](https://github.com/hyperi-io/dfe-engine/issues/210)) ([83a29f1](https://github.com/hyperi-io/dfe-engine/commit/83a29f13f502e235e60c5fb0617e3b6e120335fd))

## [1.17.2](https://github.com/hyperi-io/dfe-engine/compare/v1.17.1...v1.17.2) (2026-08-23)

### Bug Fixes

* **deps:** adopt scalo 2.29.16 -- log format derives from otel presence ([#205](https://github.com/hyperi-io/dfe-engine/issues/205)) ([35f6572](https://github.com/hyperi-io/dfe-engine/commit/35f6572d13d75e7bd657ebeece6641ee488487e6))

## [1.17.1](https://github.com/hyperi-io/dfe-engine/compare/v1.17.0...v1.17.1) (2026-08-23)

### Bug Fixes

* **deps:** adopt scalo 2.29.15 so [metrics] carries the OTel SDK ([8e3a4ce](https://github.com/hyperi-io/dfe-engine/commit/8e3a4ce63cbc98cb0efa6267b1877a532a1771b8))
* **docs:** correct engine JWKS path and issuer in LOCAL-DEV ([c5ed201](https://github.com/hyperi-io/dfe-engine/commit/c5ed2015f9de0763fb2592422648778ca83ddae7))

## [1.16.3](https://github.com/hyperi-io/dfe-engine/compare/v1.16.2...v1.16.3) (2026-08-20)

### Bug Fixes

* **auth:** reconcile named seed accounts on every boot ([#192](https://github.com/hyperi-io/dfe-engine/issues/192)) ([c6bc8de](https://github.com/hyperi-io/dfe-engine/commit/c6bc8dec6d26d332ff7dd750d6ee77bfba399f4d)), closes [#106](https://github.com/hyperi-io/dfe-engine/issues/106)

## [1.16.2](https://github.com/hyperi-io/dfe-engine/compare/v1.16.1...v1.16.2) (2026-08-20)

### Bug Fixes

* **auth:** flexible attributes on users and groups, sensitive kept separate ([#191](https://github.com/hyperi-io/dfe-engine/issues/191)) ([8cebcda](https://github.com/hyperi-io/dfe-engine/commit/8cebcdaa077fae207ea714bd746039d13355ae7e))

## [1.16.1](https://github.com/hyperi-io/dfe-engine/compare/v1.16.0...v1.16.1) (2026-08-20)

### Bug Fixes

* **auth:** document-store default for users/groups; git only for break-glass ([940377e](https://github.com/hyperi-io/dfe-engine/commit/940377e3068376aa713d7c1ce2a791cf971f55c5))
* **auth:** persist break-glass account changes to the deploy repo ([f0034bc](https://github.com/hyperi-io/dfe-engine/commit/f0034bcf147ef8f677a37b56b1c458286a241a33)), closes [#188](https://github.com/hyperi-io/dfe-engine/issues/188)

## [1.16.0](https://github.com/hyperi-io/dfe-engine/compare/v1.15.1...v1.16.0) (2026-08-20)

### Features

* **rules:** create a hunt rule from a HyperDX view (POST /rules/from-hyperdx) ([#183](https://github.com/hyperi-io/dfe-engine/issues/183)) ([86585f5](https://github.com/hyperi-io/dfe-engine/commit/86585f5705ec4e05ee4fa787a991bedb922b42ed))

## [1.15.1](https://github.com/hyperi-io/dfe-engine/compare/v1.15.0...v1.15.1) (2026-08-19)

### Bug Fixes

* **governance:** reconcile reasserts CH passwords, otel db->dfe, dex issuer backout ([aa0247f](https://github.com/hyperi-io/dfe-engine/commit/aa0247f0bdde7e61db54cc813bdb4bff1c095a26))

## [1.15.0](https://github.com/hyperi-io/dfe-engine/compare/v1.14.1...v1.15.0) (2026-08-19)

### Features

* **auth:** FerretDB-backed account store and reusable document layer ([9888ec0](https://github.com/hyperi-io/dfe-engine/commit/9888ec0b5c5ef93691c3bf4dd9a96b5d6f5b459d))

### Bug Fixes

* **hyperdx:** scope-aware query:execute gate so an org_viewer gets its connection ([2240256](https://github.com/hyperi-io/dfe-engine/commit/224025616af48dfd55ddd5ab317452b0cea0a376))

## [1.14.1](https://github.com/hyperi-io/dfe-engine/compare/v1.14.0...v1.14.1) (2026-08-19)

### Bug Fixes

* adopt scalo 2.29.14 so the collector endpoint the charts set is honoured ([d75d0ca](https://github.com/hyperi-io/dfe-engine/commit/d75d0caa3a4239f59914205d8b5202a2633ac7a0))
* **deps:** update dependency node to v24.19.0 ([#134](https://github.com/hyperi-io/dfe-engine/issues/134)) ([7bb1c59](https://github.com/hyperi-io/dfe-engine/commit/7bb1c592ac15109f99c0d953e4b218f34d3880cd))

## [1.14.0](https://github.com/hyperi-io/dfe-engine/compare/v1.13.0...v1.14.0) (2026-08-18)

## [1.13.0](https://github.com/hyperi-io/dfe-engine/compare/v1.12.0...v1.13.0) (2026-08-18)

## [1.12.0](https://github.com/hyperi-io/dfe-engine/compare/v1.11.4...v1.12.0) (2026-08-18)

## [1.11.4](https://github.com/hyperi-io/dfe-engine/compare/v1.11.3...v1.11.4) (2026-08-17)

## [1.11.3](https://github.com/hyperi-io/dfe-engine/compare/v1.11.2...v1.11.3) (2026-08-17)

## [1.11.2](https://github.com/hyperi-io/dfe-engine/compare/v1.11.1...v1.11.2) (2026-08-17)

## [1.11.1](https://github.com/hyperi-io/dfe-engine/compare/v1.11.0...v1.11.1) (2026-08-04)

## [1.11.0](https://github.com/hyperi-io/dfe-engine/compare/v1.10.7...v1.11.0) (2026-08-03)

# [1.8.0](https://github.com/hyperi-io/dfe-engine/compare/v1.7.3...v1.8.0) (2026-04-15)


### Bug Fixes

* address review findings — typed responses, path naming, auth gap ([34366d1](https://github.com/hyperi-io/dfe-engine/commit/34366d1c809b5df6c0f0a5882f5639363dfe9d64))
* allowlist example API key format in gitleaks config ([ff70edd](https://github.com/hyperi-io/dfe-engine/commit/ff70edd50fe4fa05ea712d5cf489a3c1f27568c9))
* **cel:** use bare CurrentUser annotation, FastAPI rejects Depends() default ([bd439d3](https://github.com/hyperi-io/dfe-engine/commit/bd439d3bd0c84a24ff14363c0faf3dd15e2b607c))
* migrate argo_rbac.py to use RoleConfig instead of DEFAULT_ROLE_PERMISSIONS ([2c5d1bc](https://github.com/hyperi-io/dfe-engine/commit/2c5d1bcfa5fe1588875e5197fc046d4c8d843aef))
* remove deprecated LocalAuthSettings and update remaining references ([4135a4e](https://github.com/hyperi-io/dfe-engine/commit/4135a4ebf32641edf4213c8cd0171837ee016ee8))
* remove pyarrow dependency — query module uses native JSON ([5ec23e3](https://github.com/hyperi-io/dfe-engine/commit/5ec23e383aae10d50728e4a70a1c9b7c65b34866))
* resolve ruff lint warnings in test files ([f78dd0e](https://github.com/hyperi-io/dfe-engine/commit/f78dd0e9842eaed99889d5de66f6057f2e2c379e))
* wire Google and Okta adapters into get_adapter() factory ([b049e41](https://github.com/hyperi-io/dfe-engine/commit/b049e41c7be2065f3ebb1d6efee7352b041e8dd3))


### Features

* add account, group, and API key CRUD REST endpoints ([c882c3c](https://github.com/hyperi-io/dfe-engine/commit/c882c3c8cb6e69ed82ae280f4cfb0d7d37bd264d))
* add AccountStore with YAML-backed account CRUD and bcrypt passwords ([46db7af](https://github.com/hyperi-io/dfe-engine/commit/46db7afd64c15869c9214d975a520a86a0763a7c))
* add APIKeyStore with SHA-256 hashed API keys and prefix format ([f133e57](https://github.com/hyperi-io/dfe-engine/commit/f133e573ce47c2ede16df0a576b586f8c06cedfb))
* add CLI subcommands for account, group, and API key management ([013414c](https://github.com/hyperi-io/dfe-engine/commit/013414cddb783d437e570fa6274af3b76e6e9c1c))
* add CLI subcommands for account, group, and API key management ([302d05c](https://github.com/hyperi-io/dfe-engine/commit/302d05c3b5676f7f92a3cf28d34e2c439230fc38))
* add ConnectionRegistry with multi-tenant CH custom settings pattern ([3c8baf4](https://github.com/hyperi-io/dfe-engine/commit/3c8baf4d23082eb5627305467843975d68021897))
* add dedicated_database and internal metadata to Org model ([cab11d8](https://github.com/hyperi-io/dfe-engine/commit/cab11d89f5ea219c3f7c785dd96fde6654ef37b1))
* add delete_team and update_connection to HyperDXClient ([1951c69](https://github.com/hyperi-io/dfe-engine/commit/1951c69e151de69977c7e612054c682bea635012))
* add Entra ID adapter for Graph API group resolution ([41fe5f6](https://github.com/hyperi-io/dfe-engine/commit/41fe5f6d375358468f937bacd1cacc46f5521e07))
* add external, source_provider, last_login_at to Account model ([198e2ce](https://github.com/hyperi-io/dfe-engine/commit/198e2ce543dd7e8704b94430768048a376d24fc2))
* add Google Workspace adapter for Admin SDK group resolution ([a77b04d](https://github.com/hyperi-io/dfe-engine/commit/a77b04dcd251126f87fef2fd9a205516ead85b99))
* add GroupStore with YAML-backed group CRUD and role resolution ([6440c47](https://github.com/hyperi-io/dfe-engine/commit/6440c47379889bd8e3120a577dccf49b8aa0edea))
* add HyperDX user-to-team membership via invite API ([1a525e2](https://github.com/hyperi-io/dfe-engine/commit/1a525e2e2e87ebcc76c6775015df50764658d94b))
* add JitProvisioner for shadow account creation on first OIDC login ([a319603](https://github.com/hyperi-io/dfe-engine/commit/a3196034f6407b6917c19c52cd8d07ff48f3dae1))
* add OIDC group sync runner ([7ee0cca](https://github.com/hyperi-io/dfe-engine/commit/7ee0ccae5b1d2f68fe07c814b6cbaa6169220779))
* add OIDC header and API key auth paths to get_current_user() ([3fe8f66](https://github.com/hyperi-io/dfe-engine/commit/3fe8f6630ce309352243b8752c29b8cb762bb399))
* add OIDC provider CRUD REST API and CLI subcommands ([9bfb7f4](https://github.com/hyperi-io/dfe-engine/commit/9bfb7f494d56741ecd05c9196d384981a73a575d))
* add OIDCGroupAdapter ABC and GenericAdapter ([5506d81](https://github.com/hyperi-io/dfe-engine/commit/5506d8128d2567724d0998173f258dd4919d849d))
* add OIDCProvider and GroupInfo models ([8ba6912](https://github.com/hyperi-io/dfe-engine/commit/8ba6912199254c991f17551601f7897296bfa8bf))
* add OIDCProviderRegistry with YAML-backed CRUD ([3fe81e3](https://github.com/hyperi-io/dfe-engine/commit/3fe81e31bce8bf54ddf25bef28cc2839f26d1753))
* add Okta adapter stub ([99f1788](https://github.com/hyperi-io/dfe-engine/commit/99f1788cff21121f63df22b2f7d9439a76b7f990))
* add org_ids to Group model for org membership mapping ([bb2174f](https://github.com/hyperi-io/dfe-engine/commit/bb2174ffe8c9582316b82939adb81e96871d428b))
* add OrgChProvisioner for dedicated database provisioning ([9f05a5e](https://github.com/hyperi-io/dfe-engine/commit/9f05a5e841542767cef52a8054ae9225496a427d))
* add OrgLifecycleManager for org lifecycle orchestration ([dfb8762](https://github.com/hyperi-io/dfe-engine/commit/dfb876217aafdb20298368f60d69c58a4155ae7a))
* add OrgRegistry, HyperDXClient, and coverage tests for Phase 3-4 ([c15b88d](https://github.com/hyperi-io/dfe-engine/commit/c15b88d069ede531c20b0f4212d930582e707655))
* add Phase 3 operational routers (tasks, queries, hunts, pipeline) ([a0c3da7](https://github.com/hyperi-io/dfe-engine/commit/a0c3da7f759f60b9c36257d9243daea2a3e753ba))
* add Phase 4 discovery + analytics routers (discovery, sigma, schemas) ([aa47790](https://github.com/hyperi-io/dfe-engine/commit/aa4779054ecf440a3ea948611490835ad9ce0b39))
* add RoleConfig with YAML roles and wildcard permission matching ([07bae45](https://github.com/hyperi-io/dfe-engine/commit/07bae456a5f1d7e16ba1d98eb2c8c540841b1773))
* add schema-less service surface discovery with metrics manifest ([a027697](https://github.com/hyperi-io/dfe-engine/commit/a0276972a77fa3312eef7c895dc29ce3de0910a1))
* add SOC2 audit events for org provisioning and JIT accounts ([179c534](https://github.com/hyperi-io/dfe-engine/commit/179c534c217657df363564e4a98756a63e85b272))
* add SOC2 audit logging via OTel structured log events ([c851d2e](https://github.com/hyperi-io/dfe-engine/commit/c851d2e84391099ddd917b7759e6b16f867b8832))
* add source_provider field to Group model for OIDC sync tracking ([c1577b7](https://github.com/hyperi-io/dfe-engine/commit/c1577b7e0f7bef195f5339e54ed24235435c3710))
* bootstrap auth stores, rewrite LocalAuthProvider, wire into app lifespan ([92ce0b8](https://github.com/hyperi-io/dfe-engine/commit/92ce0b834d5c49e0080aea6ec3856ca9f137e2e2))
* **cel,transport_filter:** UI helpers for CEL validation and transport filter config ([a02942b](https://github.com/hyperi-io/dfe-engine/commit/a02942b496926ce0ee531277cd2ea78de9045e8e))
* Phase 5 — regenerate OpenAPI spec (75 paths) + UI API guide ([33ae25e](https://github.com/hyperi-io/dfe-engine/commit/33ae25e916d5f78bc3b89e379457f08b3d937a75))
* refactor authorize() to use RoleConfig, migrate auth tests to new role names ([97ba046](https://github.com/hyperi-io/dfe-engine/commit/97ba04672d1198f9d66972ebe15e4bfdf88ba7b6))
* SOC2 audit remediation — wire audit_resource_change into all mutating endpoints ([3f2b7c8](https://github.com/hyperi-io/dfe-engine/commit/3f2b7c80cd6370d1f0ef76044049b3f26df02ee0))
* wire JIT provisioning into OIDC auth path ([d526a8e](https://github.com/hyperi-io/dfe-engine/commit/d526a8eb82eb51af72def2c49179729fdf24f675))
* wire OrgLifecycleManager into orgs API + app lifespan ([7455503](https://github.com/hyperi-io/dfe-engine/commit/7455503a6ab70cbe6cbc013bb07471473c7bc88b))

## [1.7.3](https://github.com/hyperi-io/dfe-engine/compare/v1.7.2...v1.7.3) (2026-03-30)


### Bug Fixes

* drop unused deps and vendor deep_merge to remove deepmerge ([12d2446](https://github.com/hyperi-io/dfe-engine/commit/12d24467095f4468c529bfb0b825698ba1538884))
* replace python-jose with PyJWT to eliminate ecdsa CVE exposure ([597edf4](https://github.com/hyperi-io/dfe-engine/commit/597edf4789e6fcc182df488410aa6a9f5a35de86)), closes [#4](https://github.com/hyperi-io/dfe-engine/issues/4)

## [1.7.2](https://github.com/hyperi-io/dfe-engine/compare/v1.7.1...v1.7.2) (2026-03-30)


### Bug Fixes

* adopt hyperi_pylib health probes for K8s ([5c8a02f](https://github.com/hyperi-io/dfe-engine/commit/5c8a02fd351cebeac1ba2cc59add094fbd8c6d52))

## [1.7.1](https://github.com/hyperi-io/dfe-engine/compare/v1.7.0...v1.7.1) (2026-03-29)


### Bug Fixes

* patch pyasn1 CVE and set pip-audit to warn for unfixable CVEs ([0d39106](https://github.com/hyperi-io/dfe-engine/commit/0d39106bcc0e26fce321983b18166916aeb6ad74))

## [1.6.9](https://github.com/hyperi-io/dfe-engine/compare/v1.6.8...v1.6.9) (2026-03-29)


### Bug Fixes

* add build.type app to CI config ([630fbcc](https://github.com/hyperi-io/dfe-engine/commit/630fbcc20b5689533d8e92f6abde59f127f45947))
* add ruff I,UP,N,PT,RUF,PIE,T20 rules, pydocstyle config ([097bf1f](https://github.com/hyperi-io/dfe-engine/commit/097bf1fd2435510a7ea7fde6db2f0d5a0884bec8))
* lower coverage threshold to 70% (current: 75%, target: 80%) ([5edc45e](https://github.com/hyperi-io/dfe-engine/commit/5edc45ec90649c5ca0d3f4325a58ea7950b4874d))
* migrate to single-branch CI and bump pylib to 2.25 ([782f439](https://github.com/hyperi-io/dfe-engine/commit/782f439b23e0f828cf57cd27ba82e0caa61e325c))
* remove TEMP test ignores, handled by hyperi-ci two-tier quality ([80dd2a1](https://github.com/hyperi-io/dfe-engine/commit/80dd2a1da1099fdbcfa504abd6620bd4d856ff66))
* resolve ruff violations for full-repo CI check ([16774e1](https://github.com/hyperi-io/dfe-engine/commit/16774e1897b79c42762195c1ea7f9037963cb14c))
* restore test rule ignores pending hyperi-ci two-tier release ([c982ab1](https://github.com/hyperi-io/dfe-engine/commit/c982ab1fc27cab2b6843681a5dcb657cf6283b91))
* restructure tests per testing standards ([b65517b](https://github.com/hyperi-io/dfe-engine/commit/b65517b04d80bef76f5f4f97626a05c4c365bb3f))
* set min_coverage to 70% in CI config (overrides hyperi-ci 80% default) ([6f26b8d](https://github.com/hyperi-io/dfe-engine/commit/6f26b8de1ff94eb41db557e5512fb683401ac71e))
* shrink ruff ignore list, test rules handled by hyperi-ci ([37f3506](https://github.com/hyperi-io/dfe-engine/commit/37f3506125c0da306529201cc5a9b50efa278079))
* trigger CI on push to release branch ([e5a978d](https://github.com/hyperi-io/dfe-engine/commit/e5a978dfff9c0bcac14c183113b148e3c5ac1e2e))
* update license to BUSL-1.1 and harden clickhouse queries ([9d1365d](https://github.com/hyperi-io/dfe-engine/commit/9d1365d46c33fa73f4f0eb1a0078f547d8390cc0))

## [1.6.9-dev.4](https://github.com/hyperi-io/dfe-engine/compare/v1.6.9-dev.3...v1.6.9-dev.4) (2026-03-24)


### Bug Fixes

* lower coverage threshold to 70% (current: 75%, target: 80%) ([5edc45e](https://github.com/hyperi-io/dfe-engine/commit/5edc45ec90649c5ca0d3f4325a58ea7950b4874d))
* remove TEMP test ignores, handled by hyperi-ci two-tier quality ([80dd2a1](https://github.com/hyperi-io/dfe-engine/commit/80dd2a1da1099fdbcfa504abd6620bd4d856ff66))
* restructure tests per testing standards ([b65517b](https://github.com/hyperi-io/dfe-engine/commit/b65517b04d80bef76f5f4f97626a05c4c365bb3f))
* set min_coverage to 70% in CI config (overrides hyperi-ci 80% default) ([6f26b8d](https://github.com/hyperi-io/dfe-engine/commit/6f26b8de1ff94eb41db557e5512fb683401ac71e))
* trigger CI on push to release branch ([e5a978d](https://github.com/hyperi-io/dfe-engine/commit/e5a978dfff9c0bcac14c183113b148e3c5ac1e2e))

## [1.6.9-dev.3](https://github.com/hyperi-io/dfe-engine/compare/v1.6.9-dev.2...v1.6.9-dev.3) (2026-03-22)


### Bug Fixes

* restore test rule ignores pending hyperi-ci two-tier release ([c982ab1](https://github.com/hyperi-io/dfe-engine/commit/c982ab1fc27cab2b6843681a5dcb657cf6283b91))
* shrink ruff ignore list, test rules handled by hyperi-ci ([37f3506](https://github.com/hyperi-io/dfe-engine/commit/37f3506125c0da306529201cc5a9b50efa278079))

## [1.6.9-dev.2](https://github.com/hyperi-io/dfe-engine/compare/v1.6.9-dev.1...v1.6.9-dev.2) (2026-03-21)


### Bug Fixes

* add ruff I,UP,N,PT,RUF,PIE,T20 rules, pydocstyle config ([097bf1f](https://github.com/hyperi-io/dfe-engine/commit/097bf1fd2435510a7ea7fde6db2f0d5a0884bec8))
* resolve ruff violations for full-repo CI check ([16774e1](https://github.com/hyperi-io/dfe-engine/commit/16774e1897b79c42762195c1ea7f9037963cb14c))

## [1.6.9-dev.1](https://github.com/hyperi-io/dfe-engine/compare/v1.6.8...v1.6.9-dev.1) (2026-03-16)


### Bug Fixes

* add build.type app to CI config ([630fbcc](https://github.com/hyperi-io/dfe-engine/commit/630fbcc20b5689533d8e92f6abde59f127f45947))

## [1.6.8](https://github.com/hyperi-io/dfe-engine/compare/v1.6.7...v1.6.8) (2026-03-10)


### Bug Fixes

* add WASM transform endpoints and update hyperi-pylib to 2.24.3 ([23e408d](https://github.com/hyperi-io/dfe-engine/commit/23e408de72631ebacaf05492443d3038bde3b23f))

## [1.6.7](https://github.com/hyperi-io/dfe-engine/compare/v1.6.6...v1.6.7) (2026-03-10)


### Bug Fixes

* add http extra to hyperi-pylib dep for AsyncHttpClient/HttpClient ([b930d39](https://github.com/hyperi-io/dfe-engine/commit/b930d39e8b045ca4e29c549e78b2de33020ada18))

## [1.6.6](https://github.com/hyperi-io/dfe-engine/compare/v1.6.5...v1.6.6) (2026-03-09)


### Bug Fixes

* clarify publish target comment in hyperi-ci config ([75a7eb7](https://github.com/hyperi-io/dfe-engine/commit/75a7eb7154496256d22fdf8c1c0c93f02bd789ad))

## [1.6.5](https://github.com/hyperi-io/dfe-engine/compare/v1.6.4...v1.6.5) (2026-03-09)


### Bug Fixes

* set ty to warn in hyperi-ci config ([d179f9e](https://github.com/hyperi-io/dfe-engine/commit/d179f9e3c06b658709ac0223c7cba0bdec4de5ba))

## [1.6.4](https://github.com/hyperi-io/dfe-engine/compare/v1.6.3...v1.6.4) (2026-03-09)


### Bug Fixes

* pass JFROG_USERNAME secret to CI workflow ([089a2a9](https://github.com/hyperi-io/dfe-engine/commit/089a2a966d4578d3b208fa2df94be93615bb136d))

## [1.6.3](https://github.com/hyperi-io/dfe-engine/compare/v1.6.2...v1.6.3) (2026-03-09)


### Bug Fixes

* move bandit B104 skip to config, pip_audit back to blocking ([b11fdf2](https://github.com/hyperi-io/dfe-engine/commit/b11fdf2dcd76648fafdba88569b05bd8a9d63408))

## [1.6.2](https://github.com/hyperi-io/dfe-engine/compare/v1.6.1...v1.6.2) (2026-03-09)


### Bug Fixes

* register requires_schemas pytest marker in pyproject.toml ([a2939cf](https://github.com/hyperi-io/dfe-engine/commit/a2939cf58add988cac2004977d81c7ab523771e7))

## [1.6.1](https://github.com/hyperi-io/dfe-engine/compare/v1.6.0...v1.6.1) (2026-03-09)


### Bug Fixes

* remove old ci submodule (replaced by hyperi-ci) ([e054275](https://github.com/hyperi-io/dfe-engine/commit/e054275737cb32ddd248865c179e7e56a2504e72))
* simplify sdist excludes, hyperi-ci handles common dirs now ([3787fb3](https://github.com/hyperi-io/dfe-engine/commit/3787fb395e4eb4f80964a769ba6dc5100ac2965e))

# [1.6.0](https://github.com/hyperi-io/dfe-engine/compare/v1.5.0...v1.6.0) (2026-03-09)


### Bug Fixes

* add nosec B104 for 0.0.0.0 bind address ([9f4e174](https://github.com/hyperi-io/dfe-engine/commit/9f4e17465d616c464f66ec4f61f8f810824482eb))
* add transform-vrl ServicePlugin [skip ci] ([8c2b185](https://github.com/hyperi-io/dfe-engine/commit/8c2b1859c58ce9f34fef8f5a45704a5894b656b2))
* align transform-vrl config model with Rust schema [skip ci] ([d48daba](https://github.com/hyperi-io/dfe-engine/commit/d48daba9f891d13aa6e064390066fafcc790e35d))
* apply ruff format to all source and test files ([d24cf98](https://github.com/hyperi-io/dfe-engine/commit/d24cf9884649c2c7f767e0af96be2cc7d17b4728))
* containerise dfe-engine API server with Dockerfile and Helm chart ([b91fa89](https://github.com/hyperi-io/dfe-engine/commit/b91fa8940087be78edb7fb4f10f14ffe95553e82))
* exclude tool/submodule dirs from sdist build ([283f0ae](https://github.com/hyperi-io/dfe-engine/commit/283f0ae37a12e5ec63562efcc3deec4f0903473d))
* migrate CI to hyperi-ci reusable workflows ([583c1cc](https://github.com/hyperi-io/dfe-engine/commit/583c1ccdd7cafb4a1dbe7c32959b6efe28cc9ecf))
* pin cryptography>=46.0.5 to address CVE (subgroup attack on SECT curves) [skip ci] ([6893bc3](https://github.com/hyperi-io/dfe-engine/commit/6893bc392ef1842de5fde6b2b6880af0e070ec1a))
* resolve ruff lint errors and update deps to latest ([7df88a1](https://github.com/hyperi-io/dfe-engine/commit/7df88a19dd6684a897e9ee0788db71640f62149a))
* set semgrep to warn mode for false positives ([faaf01e](https://github.com/hyperi-io/dfe-engine/commit/faaf01e8a58a450fe557075f70a2ad23d19bb029))
* skip schema tests when submodule not checked out ([8cd7ee6](https://github.com/hyperi-io/dfe-engine/commit/8cd7ee6a296f16f4da09235aff16818bc5b4352c))


### Features

* adaptive hunt scheduling (REFRESH AFTER) + EXPLAIN plan capture ([cbde751](https://github.com/hyperi-io/dfe-engine/commit/cbde7515ff8a3e40f198fffe05fc36eb98cc33c0))
* add dfe-schemas submodule, wire SchemaLoader resolution chain ([ebe9cb3](https://github.com/hyperi-io/dfe-engine/commit/ebe9cb3621bbb0e65222d9e425eda92abdaf106f))
* add field mapping layer — FieldMap model, registry, resolver (Phase 1) ([8761e3e](https://github.com/hyperi-io/dfe-engine/commit/8761e3e7da58baee927ae6a1e438ba365c5f4a01))
* add HuntResultSchema + RuleRewriter for dynamic hunt output ([2debc7d](https://github.com/hyperi-io/dfe-engine/commit/2debc7d559a3a1a1292ad32aa06c4f1158b5ac36))
* add SchemaManager for schema version write operations ([b8012a2](https://github.com/hyperi-io/dfe-engine/commit/b8012a2f919fdb005c9ad4e7a9cb0d0e9603a1c6))
* add ViewGenerator and generic view DDL (field mapping Phase 2+3) ([6ec3491](https://github.com/hyperi-io/dfe-engine/commit/6ec349151605f3da7fbec02abd2a00aa716f78f4))
* Apprise alert system, alert grouping + cooldown, pylib audit ([c24cf31](https://github.com/hyperi-io/dfe-engine/commit/c24cf31f5c46f8e51f190eb750eaa3809da879c5))
* DDLFileWriter — auto-generate reference DDL SQL to schemas/ddl/ ([f6f68a2](https://github.com/hyperi-io/dfe-engine/commit/f6f68a26eed6fd216ff80ae4a65efc7478874b87))
* dfe-devex config submodule + DFE_CONFIG_DIR auto-resolution ([e4fc41c](https://github.com/hyperi-io/dfe-engine/commit/e4fc41c00576d0401a8ac0d64dea3439f92381f9))
* FastAPI layer (Phases 1-2.5), hunts improvements, CEL, AI module, schema-less registries [skip ci] ([a98171f](https://github.com/hyperi-io/dfe-engine/commit/a98171f4cb48d27b9089c1d4cc4e4840c81d3db2)), closes [hi#perf](https://github.com/hi/issues/perf)
* hunt source wiring, rule model, expression validator, execution profiles ([932f5db](https://github.com/hyperi-io/dfe-engine/commit/932f5db138818337804ec52aa5fdc594aaea9c2e))
* implement HuntEngine daemon thread + even load spreading ([250ac27](https://github.com/hyperi-io/dfe-engine/commit/250ac27f112db03854bc171c94f5d13d179055ce))
* integrate field mapping into schema compiler (Phase 4) ([6becc28](https://github.com/hyperi-io/dfe-engine/commit/6becc28c289eb153752b4bf543db7d59fbd2d833))
* local auth provider — three fixed accounts for simple deploy + break-glass ([d25efdb](https://github.com/hyperi-io/dfe-engine/commit/d25efdb501bb8e99b19652ab4670915e49bf9aca))
* query fingerprinting, concurrency limits, resource hog detection, backpressure, validator update, alerts-as-source ([5efd2b4](https://github.com/hyperi-io/dfe-engine/commit/5efd2b4f8deba998dae6c250008b300f42a0bfe3))
* refactor SigmaSourceMapper to use FieldMapRegistry (Phase 5) ([3976d08](https://github.com/hyperi-io/dfe-engine/commit/3976d082c67de438cf41068f645ae29388e7c546))
* schema versioning — version tree format + expr/comment separation ([d4c83f6](https://github.com/hyperi-io/dfe-engine/commit/d4c83f6c8d54c6c1f1f07cce7a9d63fb58c58620))

# [1.5.0](https://github.com/hyperi-io/dfe-engine/compare/v1.4.0...v1.5.0) (2026-03-01)


### Features

* implement WBS Phases 1-8c — Source model, schema v2, helm compiler, Argo CD ([69d860c](https://github.com/hyperi-io/dfe-engine/commit/69d860cf6cb70dc417e762d094e83711926a71ea))

# [1.4.0](https://github.com/hyperi-io/dfe-engine/compare/v1.3.71...v1.4.0) (2026-02-28)


### Features

* add abstract service plugin layer with 6 service types ([d38e22c](https://github.com/hyperi-io/dfe-engine/commit/d38e22cf1f8a1c92cdbad81f56bb0713842d820b))
* add services module with YAML-backed config registry ([aea3c60](https://github.com/hyperi-io/dfe-engine/commit/aea3c60f24a3005dff7e3358307d50771e77ff23))
* rename hs-pylib to hyperi-pylib for HyperI rebrand ([bbc39be](https://github.com/hyperi-io/dfe-engine/commit/bbc39be02a160c6d0d915bca147fbf75dec208c6))
* replace query registry with ClickHouse parameterized views ([ab15177](https://github.com/hyperi-io/dfe-engine/commit/ab151770b7eed91098d36c7febfaf3c25cee48ac))


### BREAKING CHANGES

* All imports changed from hs_pylib to hyperi_pylib.

- Update dependency from hs-pylib to hyperi-pylib in pyproject.toml
- Rename all Python imports from hs_pylib to hyperi_pylib
- Update brand references from HyperSec to HyperI in docs/comments
- Update author/email to HyperI Team / dev@hyperi.io

## [1.3.71](https://github.com/hyperi-io/dfe-engine/compare/v1.3.70...v1.3.71) (2026-02-18)


### Bug Fixes

* Point to hyperi ([d004180](https://github.com/hyperi-io/dfe-engine/commit/d004180d363cd6a4cbd8eee3e6e6f8e3c8a11c11))

## [1.3.70](https://github.com/hyperi-io/dfe-engine/compare/v1.3.69...v1.3.70) (2026-02-17)


### Bug Fixes

* Move old resources to deprecated (now submodule) and add in type mapping ([2088a83](https://github.com/hyperi-io/dfe-engine/commit/2088a839ec3bcc1329190a67f0e8ae9deab1a6f1))

## [1.3.69](https://github.com/hyperi-io/dfe-engine/compare/v1.3.68...v1.3.69) (2026-02-16)


### Bug Fixes

* Mega unit tests for combined dataframe ([516f7e8](https://github.com/hyperi-io/dfe-engine/commit/516f7e8a7f720c2c7ca18cb5fb440bec9705892b))

## [1.3.68](https://github.com/hyperi-io/dfe-engine/compare/v1.3.67...v1.3.68) (2026-02-14)


### Bug Fixes

* Need to check NA now ([82e42f2](https://github.com/hyperi-io/dfe-engine/commit/82e42f2b246de74423717050a8f332a2d5810dd1))

## [1.3.67](https://github.com/hyperi-io/dfe-engine/compare/v1.3.66...v1.3.67) (2026-02-14)


### Bug Fixes

* Missing pass through of variable ([a93b862](https://github.com/hyperi-io/dfe-engine/commit/a93b8624bf18323e58f51658a829044c3316f598))

## [1.3.66](https://github.com/hyperi-io/dfe-engine/compare/v1.3.65...v1.3.66) (2026-02-14)


### Bug Fixes

* Small bug in duplicates of derived schema sub + moving to all unique values ([d6645d0](https://github.com/hyperi-io/dfe-engine/commit/d6645d0ae8a6281382eac5ec492ff7d4beb10740))

## [1.3.65](https://github.com/hyperi-io/dfe-engine/compare/v1.3.64...v1.3.65) (2026-02-14)


### Bug Fixes

* Adding types to schema fields ([60fe08e](https://github.com/hyperi-io/dfe-engine/commit/60fe08ef9c35b62d2e705178617b390396c0bb1c))

## [1.3.64](https://github.com/hyperi-io/dfe-engine/compare/v1.3.63...v1.3.64) (2026-02-13)


### Bug Fixes

* Allowing dataframe debugs to show 100 rows ([12514c9](https://github.com/hyperi-io/dfe-engine/commit/12514c902e21fb2cfad6564146918a5d403bd0ec))

## [1.3.63](https://github.com/hyperi-io/dfe-engine/compare/v1.3.62...v1.3.63) (2026-02-13)


### Bug Fixes

* Small bug with unaccessed var ([b7174d5](https://github.com/hyperi-io/dfe-engine/commit/b7174d5be50216eca4bf40743211246c8e310a77))

## [1.3.62](https://github.com/hyperi-io/dfe-engine/compare/v1.3.61...v1.3.62) (2026-02-13)


### Bug Fixes

* Meta schema fields now part of combine df ([dc1f0da](https://github.com/hyperi-io/dfe-engine/commit/dc1f0da1e7eb7d6e5b3eaa1cd8c6194f3de67550))

## [1.3.61](https://github.com/hyperi-io/dfe-engine/compare/v1.3.60...v1.3.61) (2026-02-13)


### Bug Fixes

* Initial push of duplicate checking in schemas ([99a1167](https://github.com/hyperi-io/dfe-engine/commit/99a1167f2e40ef25846a3eb11eed7dfc1758fc2d))

## [1.3.60](https://github.com/hyperi-io/dfe-engine/compare/v1.3.59...v1.3.60) (2026-02-10)


### Bug Fixes

* Adding new idea of PK to schemas ([afc5c77](https://github.com/hyperi-io/dfe-engine/commit/afc5c774d11526436d72e434a3f689d1049ae3ec))

## [1.3.59](https://github.com/hyperi-io/dfe-engine/compare/v1.3.58...v1.3.59) (2026-02-10)


### Bug Fixes

* Derived schemas now tested and loaded via function ([7539fcf](https://github.com/hyperi-io/dfe-engine/commit/7539fcfd79bc1c0393747c4963a2119eeb3c9531))

## [1.3.58](https://github.com/hypersec-io/dfe-engine/compare/v1.3.57...v1.3.58) (2026-02-09)


### Bug Fixes

* Some better error handling ([392ff22](https://github.com/hypersec-io/dfe-engine/commit/392ff2228230baf6acb758386debc6d103da7b01))

## [1.3.57](https://github.com/hypersec-io/dfe-engine/compare/v1.3.56...v1.3.57) (2026-02-09)


### Bug Fixes

* Updated some exception raises ([e43619c](https://github.com/hypersec-io/dfe-engine/commit/e43619cce28256ce787604409d7db1996218a399))

## [1.3.56](https://github.com/hypersec-io/dfe-engine/compare/v1.3.55...v1.3.56) (2026-02-09)


### Bug Fixes

* Some updates to custom exceptions ([dc089c7](https://github.com/hypersec-io/dfe-engine/commit/dc089c7291c4a98e556a6b24ff659517a50b0d88))

## [1.3.55](https://github.com/hypersec-io/dfe-engine/compare/v1.3.54...v1.3.55) (2026-02-09)


### Bug Fixes

* Allow list of errors to be presented when various issues with schema ([e9a61f3](https://github.com/hypersec-io/dfe-engine/commit/e9a61f308cccb10bda606a7a95925cff8cf8994b))

## [1.3.54](https://github.com/hypersec-io/dfe-engine/compare/v1.3.53...v1.3.54) (2026-02-09)


### Bug Fixes

* New constraints on meta/CH/type maps as well as easy to edit required fields ([1cc9298](https://github.com/hypersec-io/dfe-engine/commit/1cc92982c146e40e430e1ad7c780bbfe7773fead))
* New tests to support new column definitions - removed __init__ from coverage as well as internal functions ([68949f4](https://github.com/hypersec-io/dfe-engine/commit/68949f4bb1320f6a35ded83af57a2aec0c0da1e5))

## [1.3.53](https://github.com/hypersec-io/dfe-engine/compare/v1.3.52...v1.3.53) (2026-02-06)


### Bug Fixes

* Non compulsory meta schema read tests ([f873528](https://github.com/hypersec-io/dfe-engine/commit/f873528c0da6afed045b44809722e34cab264e4a))

## [1.3.52](https://github.com/hypersec-io/dfe-engine/compare/v1.3.51...v1.3.52) (2026-02-06)


### Bug Fixes

* New tests + data resources added as a submodule ([45a9e94](https://github.com/hypersec-io/dfe-engine/commit/45a9e94272910ced0bf43bfd0b70e663173374b8))
* New tests and submodule of data resources ([f312c51](https://github.com/hypersec-io/dfe-engine/commit/f312c5164999ef39a4ab56cb718c56c9ab3edaf1))

## [1.3.51](https://github.com/hypersec-io/dfe-engine/compare/v1.3.50...v1.3.51) (2026-02-03)


### Bug Fixes

* Fixed config with expanduser ([d1cbc0c](https://github.com/hypersec-io/dfe-engine/commit/d1cbc0ccb1591222d416e0a1e7df475d52a35532))

## [1.3.50](https://github.com/hypersec-io/dfe-engine/compare/v1.3.49...v1.3.50) (2026-02-03)


### Bug Fixes

* Minor config debug ([e154ae6](https://github.com/hypersec-io/dfe-engine/commit/e154ae677912e5db21ca9733e52e5d00a4710db8))

## [1.3.49](https://github.com/hypersec-io/dfe-engine/compare/v1.3.48...v1.3.49) (2026-02-03)


### Bug Fixes

* Small update to config ([f685e8c](https://github.com/hypersec-io/dfe-engine/commit/f685e8cfcdf05485cbe98408eb33f16c32da93bd))

## [1.3.48](https://github.com/hypersec-io/dfe-engine/compare/v1.3.47...v1.3.48) (2026-02-03)


### Bug Fixes

* Updates to config and CH DDL builder ([7489695](https://github.com/hypersec-io/dfe-engine/commit/74896951293073af4aa0e319e736de51f536a09a))

## [1.3.47](https://github.com/hypersec-io/dfe-engine/compare/v1.3.46...v1.3.47) (2026-02-03)


### Bug Fixes

* using loc on the df to re assign json ([636d576](https://github.com/hypersec-io/dfe-engine/commit/636d5760543c4b33c84d5d6635ebc2a01048e481))

## [1.3.46](https://github.com/hypersec-io/dfe-engine/compare/v1.3.45...v1.3.46) (2026-02-03)


### Bug Fixes

* Move JSON identification to utils ([a2b7e71](https://github.com/hypersec-io/dfe-engine/commit/a2b7e711404c74d2d090a89d9ec3269bf9885208))

## [1.3.45](https://github.com/hypersec-io/dfe-engine/compare/v1.3.44...v1.3.45) (2026-02-03)


### Bug Fixes

* Added new JSON mapping control ([483b6cb](https://github.com/hypersec-io/dfe-engine/commit/483b6cb8ab9b851519eca8d7ec93814568bbe0d9))

## [1.3.44](https://github.com/hypersec-io/dfe-engine/compare/v1.3.43...v1.3.44) (2026-02-03)


### Bug Fixes

* Unique identification pre info generation ([8e3580e](https://github.com/hypersec-io/dfe-engine/commit/8e3580ec5645c0b47c2222b9bfdc46e201d32b1f))

## [1.3.43](https://github.com/hypersec-io/dfe-engine/compare/v1.3.42...v1.3.43) (2026-02-03)


### Bug Fixes

* Forgot to add the schemas ([d7fa65a](https://github.com/hypersec-io/dfe-engine/commit/d7fa65aea8e4a50e07a6bdcd6292431665e71e85))

## [1.3.42](https://github.com/hypersec-io/dfe-engine/compare/v1.3.41...v1.3.42) (2026-02-03)


### Bug Fixes

* Duplicate check before object create ([dd183b4](https://github.com/hypersec-io/dfe-engine/commit/dd183b47a368910a335c39a873999e7d0c44d650))

## [1.3.41](https://github.com/hypersec-io/dfe-engine/compare/v1.3.40...v1.3.41) (2026-02-03)


### Bug Fixes

* Updates to the schema build ([cacf4ca](https://github.com/hypersec-io/dfe-engine/commit/cacf4ca8f372d6219e1964190d284fcea7cc62fe))

## [1.3.40](https://github.com/hypersec-io/dfe-engine/compare/v1.3.39...v1.3.40) (2026-02-03)


### Bug Fixes

* New CH DDL generator ([3a1b848](https://github.com/hypersec-io/dfe-engine/commit/3a1b848d1b0d39dfc1746b2238423aa58047c194))

## [1.3.39](https://github.com/hypersec-io/dfe-engine/compare/v1.3.38...v1.3.39) (2026-02-03)


### Bug Fixes

* Checking duplicate schemas ([33134d6](https://github.com/hypersec-io/dfe-engine/commit/33134d6711d88657c42c0a769e73403089081570))

## [1.3.38](https://github.com/hypersec-io/dfe-engine/compare/v1.3.37...v1.3.38) (2026-02-03)


### Bug Fixes

* Init error str as empty ([47d79b6](https://github.com/hypersec-io/dfe-engine/commit/47d79b62bc7d70c551254f3c3b72b580c5fde8d8))

## [1.3.37](https://github.com/hypersec-io/dfe-engine/compare/v1.3.36...v1.3.37) (2026-02-03)


### Bug Fixes

* New exception when no schemas to build ([80174c3](https://github.com/hypersec-io/dfe-engine/commit/80174c3f046081462377a663ece523d8eb5d9148))

## [1.3.36](https://github.com/hypersec-io/dfe-engine/compare/v1.3.35...v1.3.36) (2026-02-03)


### Bug Fixes

* Forcing paths to be path objs with no home tldr ([f2e0a05](https://github.com/hypersec-io/dfe-engine/commit/f2e0a0586af575fd5a071efe36edfcfcc7f2bdb2))

## [1.3.35](https://github.com/hypersec-io/dfe-engine/compare/v1.3.34...v1.3.35) (2026-01-29)


### Bug Fixes

* Initial commit for new schema builder with tests ([5b26374](https://github.com/hypersec-io/dfe-engine/commit/5b26374a60dba84cd853be833f78ee2cfa5ebf18))

## [1.3.34](https://github.com/hypersec-io/dfe-engine/compare/v1.3.33...v1.3.34) (2026-01-27)


### Bug Fixes

* New exception for unrequired config key ([1fc2b8a](https://github.com/hypersec-io/dfe-engine/commit/1fc2b8a2258f62edab32627c8d3a8f0713969a41))

## [1.3.33](https://github.com/hypersec-io/dfe-engine/compare/v1.3.32...v1.3.33) (2026-01-27)


### Bug Fixes

* Updating get to return None ([4dc2d6d](https://github.com/hypersec-io/dfe-engine/commit/4dc2d6d79be434d69abb4ebd50cbb373410f0bde))

## [1.3.32](https://github.com/hypersec-io/dfe-engine/compare/v1.3.31...v1.3.32) (2026-01-27)


### Bug Fixes

* Updates for complex key not found in get ([d4b853d](https://github.com/hypersec-io/dfe-engine/commit/d4b853d199c98bd6842265468ef5ea33971d6e7b))

## [1.3.31](https://github.com/hypersec-io/dfe-engine/compare/v1.3.30...v1.3.31) (2026-01-27)


### Bug Fixes

* New targets exception for when user tries to add non required key ([5b3422d](https://github.com/hypersec-io/dfe-engine/commit/5b3422d1847b36fefd61f34e7a718e0fe015fcc3))

## [1.3.30](https://github.com/hypersec-io/dfe-engine/compare/v1.3.29...v1.3.30) (2026-01-26)


### Bug Fixes

* Updated exception name and removed stray print ([dca0c55](https://github.com/hypersec-io/dfe-engine/commit/dca0c5550d7459903eb9ceb46c74f8de3fd19b82))

## [1.3.29](https://github.com/hypersec-io/dfe-engine/compare/v1.3.28...v1.3.29) (2026-01-24)


### Bug Fixes

* Align targets functions with config ([4f4c900](https://github.com/hypersec-io/dfe-engine/commit/4f4c9008e9819f8151f0cd7bea246143cd7083bd))

## [1.3.28](https://github.com/hypersec-io/dfe-engine/compare/v1.3.27...v1.3.28) (2026-01-24)


### Bug Fixes

* Missing custom_message in new exception ([e79d057](https://github.com/hypersec-io/dfe-engine/commit/e79d0574073edb3a277fedb84cca57044e44386b))

## [1.3.27](https://github.com/hypersec-io/dfe-engine/compare/v1.3.26...v1.3.27) (2026-01-24)


### Bug Fixes

* New custom exception for no updates to a config ([8b41add](https://github.com/hypersec-io/dfe-engine/commit/8b41add31bd3c2ecc09b2aa7ab79b858cb937699))

## [1.3.26](https://github.com/hypersec-io/dfe-engine/compare/v1.3.25...v1.3.26) (2026-01-24)


### Bug Fixes

* config_get returning dict ([201a1ed](https://github.com/hypersec-io/dfe-engine/commit/201a1ede16345fbfdce050f5000fe4c5ac8d74e3))

## [1.3.25](https://github.com/hypersec-io/dfe-engine/compare/v1.3.24...v1.3.25) (2026-01-24)


### Bug Fixes

* Immutable dict causing issues ([696911b](https://github.com/hypersec-io/dfe-engine/commit/696911bf74180ff648407236df65736a78467ec1))

## [1.3.24](https://github.com/hypersec-io/dfe-engine/compare/v1.3.23...v1.3.24) (2026-01-24)


### Bug Fixes

* New exception for config ([bc0d713](https://github.com/hypersec-io/dfe-engine/commit/bc0d7133ce6de275cc94e268bc45f4ea25b0f5b4))

## [1.3.23](https://github.com/hypersec-io/dfe-engine/compare/v1.3.22...v1.3.23) (2026-01-24)


### Bug Fixes

* New config has key function ([e45fad0](https://github.com/hypersec-io/dfe-engine/commit/e45fad0824ddd04115448b6cb51e1bab039a1556))

## [1.3.22](https://github.com/hypersec-io/dfe-engine/compare/v1.3.21...v1.3.22) (2026-01-24)


### Bug Fixes

* Add ability to write to a key in config update ([cffd615](https://github.com/hypersec-io/dfe-engine/commit/cffd6159678f75c8a0cf82f4374c9c5132807e3a))

## [1.3.21](https://github.com/hypersec-io/dfe-engine/compare/v1.3.20...v1.3.21) (2026-01-22)


### Bug Fixes

* Added test for config as well as minor patches for targets ([72d74f1](https://github.com/hypersec-io/dfe-engine/commit/72d74f1eac62c56c50dee1fdbe814d08a0647746))

## [1.3.20](https://github.com/hypersec-io/dfe-engine/compare/v1.3.19...v1.3.20) (2026-01-22)


### Bug Fixes

* Move to only allow path spec ([ac5ff68](https://github.com/hypersec-io/dfe-engine/commit/ac5ff68286a7f4a38aa29d977c68c8e72f5da607))

## [1.3.19](https://github.com/hypersec-io/dfe-engine/compare/v1.3.18...v1.3.19) (2026-01-21)


### Bug Fixes

* Including subdir in Config object ([91cefea](https://github.com/hypersec-io/dfe-engine/commit/91cefea1905102a246aff9ce6b860f74264de68a))

## [1.3.18](https://github.com/hypersec-io/dfe-engine/compare/v1.3.17...v1.3.18) (2026-01-21)


### Bug Fixes

* Use config_subdir_name to string together config dir ([7b1944f](https://github.com/hypersec-io/dfe-engine/commit/7b1944f8c5eb2f404f7b0c8ac9a3a2173449f417))

## [1.3.17](https://github.com/hypersec-io/dfe-engine/compare/v1.3.16...v1.3.17) (2026-01-21)


### Bug Fixes

* Move to just raise config keyerror ([68b2031](https://github.com/hypersec-io/dfe-engine/commit/68b2031ff6cf4f9ee2eb2eba757775e6a093c137))

## [1.3.16](https://github.com/hypersec-io/dfe-engine/compare/v1.3.15...v1.3.16) (2026-01-21)


### Bug Fixes

* Preventing config from creating config directory ([897c2de](https://github.com/hypersec-io/dfe-engine/commit/897c2dee7a7f8b53f7abcaf887dc02341de7595b))

## [1.3.15](https://github.com/hypersec-io/dfe-engine/compare/v1.3.14...v1.3.15) (2026-01-21)


### Bug Fixes

* Adding config key extract ([8f7f210](https://github.com/hypersec-io/dfe-engine/commit/8f7f210338ff0c4e0634a4b46b0efd395ce95f9f))

## [1.3.14](https://github.com/hypersec-io/dfe-engine/compare/v1.3.13...v1.3.14) (2026-01-20)


### Bug Fixes

* Deprecating older code and updates to include new config module ([0667a41](https://github.com/hypersec-io/dfe-engine/commit/0667a4137fbfe436eb1435c2a946362e4c9385fb))

## [1.3.13](https://github.com/hypersec-io/dfe-engine/compare/v1.3.12...v1.3.13) (2026-01-20)


### Bug Fixes

* New concept of target warning that is not detrimental to execution ([a431181](https://github.com/hypersec-io/dfe-engine/commit/a43118125145146692574f029a189b5700a17805))

## [1.3.12](https://github.com/hypersec-io/dfe-engine/compare/v1.3.11...v1.3.12) (2026-01-20)


### Bug Fixes

* New error for target not needing an update ([8986ea6](https://github.com/hypersec-io/dfe-engine/commit/8986ea6f35bdaeb512384f17c21a1531012595b6))

## [1.3.11](https://github.com/hypersec-io/dfe-engine/compare/v1.3.10...v1.3.11) (2026-01-20)


### Bug Fixes

* New exception for target keys ([43d56c8](https://github.com/hypersec-io/dfe-engine/commit/43d56c8c8dfb906267761cfa388138361456bb38))

## [1.3.10](https://github.com/hypersec-io/dfe-engine/compare/v1.3.9...v1.3.10) (2026-01-20)


### Bug Fixes

* Allow custom messages in targets errors ([0b385ad](https://github.com/hypersec-io/dfe-engine/commit/0b385ad13da07bdcebc274d55dce2153b03c9a58))

## [1.3.9](https://github.com/hypersec-io/dfe-engine/compare/v1.3.8...v1.3.9) (2026-01-20)


### Bug Fixes

* New target get data function ([c2b2651](https://github.com/hypersec-io/dfe-engine/commit/c2b2651243efaca44958d287ab3e60f199d23cec))

## [1.3.8](https://github.com/hypersec-io/dfe-engine/compare/v1.3.7...v1.3.8) (2026-01-20)


### Bug Fixes

* New exception ([ec04f8b](https://github.com/hypersec-io/dfe-engine/commit/ec04f8b1fca595597d07186d30055b35d97e71a8))

## [1.3.7](https://github.com/hypersec-io/dfe-engine/compare/v1.3.6...v1.3.7) (2026-01-20)


### Bug Fixes

* Updates to targets exceptions ([ff2ef83](https://github.com/hypersec-io/dfe-engine/commit/ff2ef831b6d1a65b3b5eb2ce4f6ec808745bda24))

## [1.3.6](https://github.com/hypersec-io/dfe-engine/compare/v1.3.5...v1.3.6) (2026-01-19)


### Bug Fixes

* Update uv lock ([63e2035](https://github.com/hypersec-io/dfe-engine/commit/63e2035d131cf189f78bc1e4764f898c11b88cac))

## [1.3.5](https://github.com/hypersec-io/dfe-engine/compare/v1.3.4...v1.3.5) (2026-01-19)


### Bug Fixes

* Update engine to use pylib 2.14.6 ([4a351e7](https://github.com/hypersec-io/dfe-engine/commit/4a351e71211087f6d6c89e96224368c28a1190f0))

## [1.3.4](https://github.com/hypersec-io/dfe-engine/compare/v1.3.3...v1.3.4) (2026-01-19)


### Bug Fixes

* Uses hs-pylib 2.14.5 ([0a7f491](https://github.com/hypersec-io/dfe-engine/commit/0a7f4916da71cb135970fe21d477b080b1ad6eed))

## [1.3.3](https://github.com/hypersec-io/dfe-engine/compare/v1.3.2...v1.3.3) (2026-01-19)


### Bug Fixes

* Using hs-pyblib 2.14.3 ([ec1d74e](https://github.com/hypersec-io/dfe-engine/commit/ec1d74ea5345c611f00b51114913e246d36daa00))

# [1.3.0](https://github.com/hypersec-io/dfe-engine/compare/v1.2.0...v1.3.0) (2026-01-16)


### Features

* add pagination, storage adapters, and documentation diagrams ([879ce4b](https://github.com/hypersec-io/dfe-engine/commit/879ce4b53815ee2cd3c2592d07adfbd88a19a5c4))

# [1.2.0](https://github.com/hypersec-io/dfe-engine/compare/v1.1.3...v1.2.0) (2026-01-16)


### Features

* add unified Query API with Arrow IPC wire format ([2a6e9de](https://github.com/hypersec-io/dfe-engine/commit/2a6e9de8d2ee32e83d63fadb95b3a06e98bb654a))

## [1.1.3](https://github.com/hypersec-io/dfe-engine/compare/v1.1.2...v1.1.3) (2025-12-16)


### Bug Fixes

* update ci submodules with auto-update config ([1ae4cc6](https://github.com/hypersec-io/dfe-engine/commit/1ae4cc6c12bd3555ddb4e2009e24dacac7753755))

## [1.1.2](https://github.com/hypersec-io/dfe-engine/compare/v1.1.1...v1.1.2) (2025-12-08)


### Bug Fixes

* refactor complex functions and remove code TODOs ([31c1773](https://github.com/hypersec-io/dfe-engine/commit/31c1773d770d941ee409b852e2d988b322f5a3d7))

## [1.1.1](https://github.com/hypersec-io/dfe-engine/compare/v1.1.0...v1.1.1) (2025-12-08)


### Bug Fixes

* move ClickHouse integration tests to tests/integration ([7e6f28b](https://github.com/hypersec-io/dfe-engine/commit/7e6f28b6a4cea1db7a1985ec60af0e851b7f82fb))

# [1.1.0](https://github.com/hypersec-io/dfe-engine/compare/v1.0.0...v1.1.0) (2025-12-08)


### Bug Fixes

* resolve CI test failures and linting issues ([dbec295](https://github.com/hypersec-io/dfe-engine/commit/dbec295990bcd21a3622875a2c23e6e7634252d7))


### Features

* remove legacy dfe_logger and logger passthrough patterns ([e4e6137](https://github.com/hypersec-io/dfe-engine/commit/e4e6137617a3c45be2b94eed5bd40f6f280e0672))

# 1.0.0 (2025-12-05)


### Features

* initial dfe-engine structure with modules from dfe-cli-core ([5431956](https://github.com/hypersec-io/dfe-engine/commit/543195637f99eb2b24f2b9f65d76eb72b8d9f17b))
