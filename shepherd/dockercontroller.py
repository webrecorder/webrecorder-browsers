import docker
import os
import base64
import time
import redis
import yaml
import random
import traceback

from redis.exceptions import BusyLoadingError


#=============================================================================
class DockerController(object):
    def _load_config(self):
        config = os.environ.get('BROWSER_CONFIG', './config.yaml')
        with open(config) as fh:
            config = yaml.load(fh)

        config = config['browser_config']
        for n, v in config.items():
            new_v = os.environ.get(n)
            if not new_v:
                new_v = os.environ.get(n.upper())

            if new_v:
                print('Setting Env Val: {0}={1}'.format(n, new_v))
                config[n] = new_v

        return config

    def __init__(self):
        config = self._load_config()

        self.name = config['cluster_name']
        self.label_name = config['label_name']

        self.init_req_expire_secs = config['init_req_expire_secs']
        self.queue_expire_secs = config['queue_expire_secs']

        self.remove_expired_secs = config['remove_expired_secs']

        self.api_version = config['api_version']

        self.ports = config['ports']
        self.port_bindings = dict((int(port), None) for port in self.ports.values())

        self.max_containers = config['max_containers']

        self.throttle_expire_secs = config['throttle_expire_secs']

        self.browser_image_prefix = config['browser_image_prefix']

        self.label_browser = config['label_browser']
        self.label_prefix = config['label_prefix']

        self.network_name = config['network_name']
        self.volume_source = config['browser_volumes']
        self.shm_size = config['shm_size']

        self.default_browser = config['default_browser']

        self._init_cli()

        while True:
            try:
                self._init_redis(config)
                break
            except BusyLoadingError:
                print('Waiting for Redis to Load...')
                time.sleep(5)

    def _init_cli(self):
        self.cli = docker.from_env()

    def _init_redis(self, config):
        redis_url = os.environ['REDIS_BROWSER_URL']

        self.redis = redis.StrictRedis.from_url(redis_url, decode_responses=True)

        self.redis.setnx('next_client', '1')
        self.redis.setnx('max_containers', self.max_containers)
        self.redis.setnx('num_containers', '0')

        # TODO: support this
        #self.redis.set('cpu_auto_adjust', config['cpu_auto_adjust'])

        # if num_containers is invalid, reset to 0
        try:
            assert(int(self.redis.get('num_containers') >= 0))
        except:
            self.redis.set('num_containers', 0)

        self.redis.set('throttle_samples', config['throttle_samples'])

        self.redis.set('throttle_max_avg', config['throttle_max_avg'])

        self.duration = int(config['container_expire_secs'])
        self.redis.set('container_expire_secs', self.duration)

    def load_avail_browsers(self, params=None):
        filters = {"dangling": False}

        if params:
            all_filters = []
            for k, v in params.items():
                if k not in ('short'):
                    all_filters.append(self.label_prefix + k + '=' + v)
            filters["label"] = all_filters
        else:
            filters["label"] = self.label_browser

        browsers = {}
        try:
            images = self.cli.images.list(filters=filters)

            for image in images:
                id_ = self._get_primary_id(image.tags)
                if not id_:
                    continue

                props = self._browser_info(image.labels)
                props['id'] = id_

                browsers[id_] = props

        except:
            traceback.print_exc()

        return browsers

    def _get_primary_id(self, tags):
        if not tags:
            return None

        primary_tag = None
        for tag in tags:
            if not tag:
                continue

            if tag.endswith(':latest'):
                tag = tag.replace(':latest', '')

            if not tag.startswith(self.browser_image_prefix):
                continue

            # pick the longest tag as primary tag
            if not primary_tag or len(tag) > len(primary_tag):
                primary_tag = tag

        if primary_tag:
            return primary_tag[len(self.browser_image_prefix):]
        else:
            return None

    def get_browser_info(self, name, include_icon=False):
        tag = self.browser_image_prefix + name

        try:
            image = self.cli.images.get(tag)
            props = self._browser_info(image.labels, include_icon=include_icon)
            props['id'] = self._get_primary_id(image.tags)
            props['tags'] = image.tags
            return props

        except:
            traceback.print_exc()
            return {}

    def _browser_info(self, labels, include_icon=False):
        props = {}
        caps = []
        for n, v in labels.items():
            wr_prop = n.split(self.label_prefix)
            if len(wr_prop) != 2:
                continue

            name = wr_prop[1]

            if not include_icon and name == 'icon':
                continue

            props[name] = v

            if name.startswith('caps.'):
                caps.append(name.split('.', 1)[1])

        props['caps'] = ', '.join(caps)

        return props

    def _get_port(self, info, port):
        info = info['NetworkSettings']['Ports'][str(port) + '/tcp']
        info = info[0]
        return info['HostPort']

    def sid(self, id):
        return id[:12]

    def timed_new_container(self, browser, env, host, reqid, audio, opts):
        start = time.time()
        info = self.new_container(browser, env, host, audio=audio, opts=opts)
        end = time.time()
        dur = end - start

        time_key = 't:' + reqid
        self.redis.setex(time_key, self.throttle_expire_secs, dur)

        throttle_samples = int(self.redis.get('throttle_samples'))
        print('INIT DUR: ' + str(dur))
        self.redis.lpush('init_timings', time_key)
        self.redis.ltrim('init_timings', 0, throttle_samples - 1)

        return info

    def new_container(self, browser_id, env=None, default_host=None, audio=None, opts=None):
        #browser = self.browsers.get(browser_id)
        browser = self.get_browser_info(browser_id)

        # get default browser
        if not browser:
            browser = self.get_browser_info(browser_id)
            #browser = self.browsers.get(self.default_browser)

        if browser.get('req_width'):
            env['SCREEN_WIDTH'] = browser.get('req_width')

        if browser.get('req_height'):
            env['SCREEN_HEIGHT'] = browser.get('req_height')

        image = browser['tags'][0]
        print('Launching ' + image)

        try:
            container = self.create_container(image, env, opts)

            info, ip = self.get_ip(container)

            # container.short_id is only 10 in length for some reason
            short_id = self.sid(container.id)

            self.redis.hset('all_containers', short_id, ip)

            result = {}

            for port_name in self.ports:
                result[port_name + '_port'] = self._get_port(info, self.ports[port_name])

            result['id'] = short_id
            result['ip'] = ip
            result['audio'] = audio
            return result

        except Exception as e:
            traceback.print_exc()
            if short_id:
                print('EXCEPTION: ' + short_id)
                self.remove_container(short_id)

            return {}

    def get_ip(self, container):
        # wait for ip to be available
        while True:
            try:
                container.reload()

                info = container.attrs

                ip = info['NetworkSettings']['IPAddress']
                if not ip:
                    ip = info['NetworkSettings']['Networks'][self.network_name]['IPAddress']

                return info, ip
            except:
                print('Waiting for init')
                time.sleep(0.2)
                pass

    def create_container(self, image, env, opts):
        if self.volume_source:
            volumes_from = [self.volume_source]
        else:
            volumes_from = None

        network_name = opts.get('network_name') or self.network_name

        container = self.cli.containers.run(image=image,
                                              detach=True,
                                              ports=self.port_bindings,
                                              network=network_name,
                                              environment=env,
                                              labels={self.label_name: self.name},
                                              volumes_from=volumes_from,
                                              shm_size=self.shm_size,
                                              cap_add=['ALL'],
                                              security_opt=['apparmor=unconfined'],
                                              #auto_remove=True,
                                              )

        return container

    def remove_container(self, short_id):
        print('REMOVING: ' + short_id)
        try:
            container = self.cli.containers.get(short_id)
            container.remove(force=True, v=True)
            #self.cli.remove_container(short_id, force=True)
        except Exception as e:
            print(e)

        reqid = None
        ip = self.redis.hget('all_containers', short_id)
        if ip:
            reqid = self.redis.hget('ip:' + ip, 'reqid')

        with redis.utils.pipeline(self.redis) as pi:
            pi.delete('ct:' + short_id)

            pi.hdel('all_containers', short_id)

            if ip:
                pi.delete('ip:' + ip)

            if reqid:
                pi.delete('req:' + reqid)

    def event_loop(self):
        for event in self.cli.events(decode=True):
            try:
                self.handle_docker_event(event)
            except Exception as e:
                print(e)

    def handle_docker_event(self, event):
        if event['Type'] != 'container':
            return

        if (event['status'] == 'die' and
            event['from'].startswith(self.browser_image_prefix) and
            event['Actor']['Attributes'].get(self.label_name) == self.name):

            short_id = self.sid(event['id'])
            print('EXITED: ' + short_id)

            #auto remove
            self.remove_container(short_id)
            self.redis.decr('num_containers')
            return

        if (event['status'] == 'start' and
            event['from'].startswith(self.browser_image_prefix) and
            event['Actor']['Attributes'].get(self.label_name) == self.name):

            short_id = self.sid(event['id'])
            print('STARTED: ' + short_id)

            self.redis.incr('num_containers')
            self.redis.setex('ct:' + short_id, self.duration, 1)
            return

    def remove_expired_loop(self):
        while True:
            try:
                self.remove_expired()
            except Exception as e:
                print(e)

            time.sleep(self.remove_expired_secs)

    def remove_expired(self):
        all_known_ids = self.redis.hkeys('all_containers')

        #all_containers = {self.sid(c['Id']) for c in self.cli.containers.get(quiet=True)}
        all_containers = [self.sid(c.id) for c in self.cli.containers.list(sparse=True, all=True)]

        for short_id in all_known_ids:
            if not self.redis.get('ct:' + short_id):
                print('TIME EXPIRED: ' + short_id)
                self.remove_container(short_id)
            elif short_id not in all_containers:
                print('STALE ID: ' + short_id)
                self.remove_container(short_id)

    def auto_adjust_max(self):
        print('Auto-Adjust Max Loop')
        try:
            scale = self.redis.get('cpu_auto_adjust')
            if not scale:
                return

            info = self.cli.info()
            cpus = int(info.get('NCPU', 0))
            if cpus <= 1:
                return

            total = int(float(scale) * cpus)
            self.redis.set('max_containers', total)

        except Exception as e:
            traceback.print_exc()

    def add_new_client(self, reqid):
        client_id = self.redis.incr('clients')
        #enc_id = base64.b64encode(os.urandom(27)).decode('utf-8')
        self.redis.setex('cm:' + reqid, self.queue_expire_secs, client_id)
        self.redis.setex('q:' + str(client_id), self.queue_expire_secs, 1)
        return client_id

    def _make_reqid(self):
        return base64.b32encode(os.urandom(15)).decode('utf-8')

    def _make_vnc_pass(self):
        return base64.b64encode(os.urandom(21)).decode('utf-8')

    def register_request(self, container_data):
        reqid = self._make_reqid()

        container_data['reqid'] = reqid

        self.redis.hmset('req:' + reqid, container_data)
        self.redis.expire('req:' + reqid, self.init_req_expire_secs)
        return reqid

    def am_i_next(self, reqid):
        client_id = self.redis.get('cm:' + reqid)

        if not client_id:
            client_id = self.add_new_client(reqid)
        else:
            self.redis.expire('cm:' + reqid, self.queue_expire_secs)

        client_id = int(client_id)
        next_client = int(self.redis.get('next_client'))

        # not next client
        if client_id != next_client:
            # if this client expired, delete it from queue
            if not self.redis.get('q:' + str(next_client)):
                print('skipping expired', next_client)
                self.redis.incr('next_client')

            # missed your number somehow, get a new one!
            if client_id < next_client:
                client_id = self.add_new_client(reqid)

        diff = client_id - next_client

        if self.throttle():
            self.redis.expire('q:' + str(client_id), self.queue_expire_secs)
            return client_id - next_client

        #num_containers = self.redis.hlen('all_containers')
        num_containers = int(self.redis.get('num_containers'))

        max_containers = self.redis.get('max_containers')
        max_containers = int(max_containers) if max_containers else self.max_containers

        if diff <= (max_containers - num_containers):
            self.redis.incr('next_client')
            return -1

        else:
            self.redis.expire('q:' + str(client_id), self.queue_expire_secs)
            return client_id - next_client

    def throttle(self):
        timings = self.redis.lrange('init_timings', 0, -1)
        if not timings:
            return False

        timings = self.redis.mget(*timings)

        avg = 0
        count = 0
        for val in timings:
            if val is not None:
                avg += float(val)
                count += 1

        if count == 0:
            return False

        avg = avg / count

        print('AVG: ', avg)
        throttle_max_avg = float(self.redis.get('throttle_max_avg'))
        if avg >= throttle_max_avg:
            print('Throttling, too slow...')
            return True

        return False

    def _copy_env(self, env, name, override=None):
        env[name] = override or os.environ.get(name)

    def get_container_info(self, reqid):
        req_key = 'req:' + reqid

        container_data = self.redis.hgetall(req_key)

        return req_key, container_data

    def init_new_browser(self, reqid, host, width=None, height=None, audio='opus'):
        req_key, container_data = self.get_container_info(reqid)

        if not container_data:
            return None

        # already started, attempt to reconnect
        if 'queue' in container_data:
            container_data['ttl'] = self.redis.ttl('ct:' + container_data['id'])
            return container_data


        queue_pos = self.am_i_next(reqid)

        if queue_pos >= 0:
            return {'queue': queue_pos}

        browser = container_data['browser']
        url = container_data.get('url', 'about:blank')
        ts = container_data.get('request_ts')

        opts = {'network_name': container_data.get('network_name', self.network_name)}

        env = {}
        env['URL'] = url
        env['TS'] = ts
        env['BROWSER'] = browser
        env["AUDIO_TYPE"] = audio or ''

        vnc_pass = self._make_vnc_pass()
        env['VNC_PASS'] = vnc_pass

        self._copy_env(env, 'PROXY_HOST', container_data.get('proxy_host'))
        self._copy_env(env, 'PROXY_PORT')
        self._copy_env(env, 'PROXY_GET_CA')
        self._copy_env(env, 'SCREEN_WIDTH', width)
        self._copy_env(env, 'SCREEN_HEIGHT', height)
        self._copy_env(env, 'IDLE_TIMEOUT')

        info = self.timed_new_container(browser, env, host, reqid, audio, opts=opts)
        info['queue'] = 0
        info['vnc_pass'] = vnc_pass

        new_key = 'ip:' + info['ip']

        # TODO: support different durations?
        self.duration = int(self.redis.get('container_expire_secs'))

        with redis.utils.pipeline(self.redis) as pi:
            pi.rename(req_key, new_key)
            pi.persist(new_key)

            pi.hmset(req_key, info)
            pi.expire(req_key, self.duration)

        info['ttl'] = self.duration
        return info

    def remove_browser(self, reqid):
        short_id = self.redis.hget('req:' + reqid, 'id')

        try:
            container = self.cli.containers.get(short_id)
        except Exception as e:
            print('Container Not Found: ' + short_id)
            return False

        try:
            container.remove(force=True)
        except Exception as e:
            print('Remove error: ' + str(e))
            return False

        return True

    def clone_browser(self, reqid, id_, name):
        short_id = self.redis.hget('req:' + reqid, 'id')

        try:
            container = self.cli.containers.get(short_id)
        except Exception as e:
            print('Container Not Found: ' + short_id)
            return {'error': str(e)}

        env = {}
        self._copy_env(env, 'PROXY_HOST')
        self._copy_env(env, 'PROXY_PORT')
        self._copy_env(env, 'PROXY_GET_CA')
        self._copy_env(env, 'SCREEN_WIDTH')
        self._copy_env(env, 'SCREEN_HEIGHT')
        self._copy_env(env, 'IDLE_TIMEOUT')
        self._copy_env(env, 'AUDIO_TYPE')

        env_list = []
        for n, v in env.items():
            if n and v:
                env_list.append(n + '=' + v)

        config = {'Env': env_list,
                  'Labels': {'wr.name': name}}

        try:
            res = container.exec_run(cmd="bash -c 'kill $(cat /tmp/browser_pid)'")

            time.sleep(0.5)

        except Exception as e:
            print(e)

        try:
            res = container.commit(repository='oldwebtoday/user/' + id_,
                                   conf=config)

            return {'success': '1'}
        except Exception as e:
            print(e)
            return {'error': str(e)}

    def get_random_browser(self):
        browsers = self.load_avail_browsers()
        while True:
            id_ = random.choice(browsers.keys())
            if browsers[id_].get('skip_random'):
                continue

            return id_
