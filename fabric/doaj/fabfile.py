import sys, os, time
from fabric.api import env, run, sudo, cd, abort, roles, execute, warn_only

env.use_ssh_config = True  # username, identity file (key), hostnames for machines will all be loaded from ~/.ssh/config
# You can run this script like this:
# fab -H <host1,host2> <task_name>

# your local ssh config does not apply when the script is explicitly specifying which hosts to run tasks on...
# so username and key path will still have to be set here, or specified on the command line using -u <username> and -i <path to key>
env.user = 'cloo'  
if not env.get('key_filename'):
    # env.setdefault does not seem to work correctly
    env.key_filename = []
env.key_filename.extend(
    [
        '~/.ssh/cl',
        # add the path to your own private key if you wish
        # you can also add the -i argument when running fabric:
        # fab -i <path to key> <task_name>:arg1=value1,arg2=value2
    ]
)


DOAJGATE_IP = '46.101.12.197'
DOAJAPP1_IP = '46.101.38.194'
DOAJ_TEST_IP = '178.62.92.200'
DOAJ_STAGING_IP = '95.85.48.213'
APP_SERVER_NAMES = {'DOAJGATE': DOAJGATE_IP}  # the gateway nginx config files are named after which app server the gateway directs traffic to
TEST_SERVER_NAMES = {'DOAJ_TEST': DOAJ_TEST_IP}
STAGING_SERVER_NAMES = {'DOAJ_STAGING': DOAJ_STAGING_IP}

STAGING_DO_NAME = 'doaj-staging'

env.hosts = [DOAJGATE_IP]

DOAJ_PROD_PATH_SRC = '/home/cloo/repl/apps/doaj/src/doaj'
DOAJ_TEST_PATH_SRC = '/home/cloo/repl/test/doaj/src/doaj'
DOAJ_USER_APP_PORT = 5050

# Gateway server - nginx site config filename bits (also includes the server name in the middle)
GATE_NGINX_CFG_PREFIX = 'doaj-forward-to-'
GATE_NGINX_CFG_SUFFIX = '-server-with-local-static'

# Used when running tasks directly, e.g. fab update_doaj . Not (yet)
# used when a task like switch_doaj is calling multiple other tasks
# programmatically. Enables us to not have to specify "which hosts" 
# all the time when running Fabric.
env.roledefs.update(
        {
            'app': [DOAJAPP1_IP],
            'gate': [DOAJGATE_IP],
            'test': [DOAJ_TEST_IP],
            'staging': [DOAJ_STAGING_IP]
        }
)

@roles('gate')
def update_doaj(branch='production', tag="", doajdir=DOAJ_PROD_PATH_SRC, deploy_script='production_doaj_deploy-gateway.sh'):

    if not tag and branch == 'production':
        print 'Please specify a tag to deploy to production'
        sys.exit(1)

    if doajdir == DOAJ_PROD_PATH_SRC and branch != 'production':
        print 'You\'re deploying something other than the production branch to the live DOAJ app location.'
        print 'Aborting execution. If you really want to do this edit this script and comment the guard out.'
        sys.exit(1)

    with cd(doajdir):
        run('git config user.email "us@cottagelabs.com"')
        run('git config user.name "Cottage Labs LLP"')
        run('git checkout master')  # so that we have a branch we can definitely pull in
                                    # (in case we start from one that was set up for pushing only)
        run('git pull', pty=False)  # get any new branches
        run('git checkout ' + branch)
        run('git pull', pty=False)  # again, in case the checkout actually switched the branch, pull from the remote now
        if tag:
            run('git checkout {0}'.format(tag))
        run('git submodule update --init', pty=False)
        run('deploy/' + deploy_script)

@roles('staging')
def reload_staging():
    execute(reload_webserver, supervisor_doaj_task_name='doaj-staging', hosts=env.roledefs['staging'])

def _get_hosts(from_, to_):
    FROM = from_.upper()
    TO = to_.upper()
    if FROM not in APP_SERVER_NAMES or TO not in APP_SERVER_NAMES:
        abort('When syncing suggestions from one machine to another, only the following server names are valid: ' + ', '.join(APP_SERVER_NAMES))
    source_host = APP_SERVER_NAMES[FROM]
    target_host = APP_SERVER_NAMES[TO]
    return FROM, source_host, TO, target_host
    
@roles('app')
def push_xml_uploads():
    # TODO: move the hardcoded dirs and files to python constants to top
    # of file .. bit pointless for now as the scheduled backups
    # themselves have those bits hardcoded too
    run("/home/cloo/backups/backup2s3.sh {doaj_path}/upload/ /home/cloo/backups/doaj-xml-uploads/ dummy s3://doaj-xml-uploads >> /home/cloo/backups/logs/doaj-xml-uploads_`date +%F_%H%M`.log 2>&1"
            .format(doaj_path=DOAJ_PROD_PATH_SRC),
        pty=False
    )

@roles('app')
def pull_xml_uploads():
    # TODO: same as push_xml_uploads
    run("/home/cloo/backups/restore_from_s3.sh s3://doaj-xml-uploads {doaj_path}/upload/ /home/cloo/backups/doaj-xml-uploads/"
            .format(doaj_path=DOAJ_PROD_PATH_SRC),
        pty=False
    )

@roles('app')
def check_doaj_running():
    run('if [ $(curl -s localhost:{app_port}| grep {check_for} | wc -l) -ge 1 ]; then echo "DOAJ running on localhost:{app_port}"; fi'
            .format(app_port=DOAJ_USER_APP_PORT, check_for="doaj")
    )

@roles('app')
def print_doaj_app_config():
    print_keys = {
            'secret_settings.py': ['RECAPTCHA'],
            'settings.py': ['DOMAIN', 'SUPPRESS_ERROR_EMAILS', 'DEBUG']
    }
    for file_, keys in print_keys.items():
        for key in keys:
            run('grep {key} {doaj_path}/portality/{file_}'.format(file_=file_, key=key, doaj_path=DOAJ_PROD_PATH_SRC))

@roles('app')
def reload_webserver(supervisor_doaj_task_name='doaj-production'):
    sudo('kill -HUP $(sudo supervisorctl pid {0})'.format(supervisor_doaj_task_name))

@roles('gate')
def deploy_live(branch='production', tag=""):
    update_doaj(branch=branch, tag=tag)
    execute(reload_webserver, hosts=env.roledefs['app'])

@roles('gate')
def deploy_staging(branch='production', tag=""):
    update_doaj(branch=branch, tag=tag, doajdir=DOAJ_TEST_PATH_SRC, deploy_script='deploy/production_doaj_deploy-gateway.sh')
    execute(reload_webserver(supervisor_doaj_task_name='doaj-test'), hosts=env.roledefs['test'])

@roles('gate')
def deploy_test(branch='develop', tag=""):
    update_doaj(branch=branch, tag=tag, doajdir=DOAJ_TEST_PATH_SRC, deploy_script='test_doaj_deploy-gateway.sh')
    execute(reload_webserver(supervisor_doaj_task_name='doaj-test'), hosts=env.roledefs['test'])

@roles('gate')
def create_staging(tag):
    if _find_staging_server(STAGING_DO_NAME):
        print "The staging server already exists. Destroy it with destroy_staging task first if you want, then rerun this."
        sys.exit(1)
    public_ip, private_ip = _create_staging_server(STAGING_DO_NAME)
    execute(_setup_gate_for_staging(private_ip), hosts=[DOAJGATE_IP])
    execute(_setup_staging_server, hosts=[public_ip])
    print 'Staging server set up complete. SSH into {0}'.format(public_ip)

def destroy_staging():
    staging = _find_staging_server(STAGING_DO_NAME)
    if not staging:
        print "Can't find a droplet with the name {0}, so nothing to do.".format(STAGING_DO_NAME)
        sys.exit(0)
    print 'Destroying {0} in 5 seconds. Ctrl+C if you want to stop this.'.format(STAGING_DO_NAME)
    time.sleep(5)
    staging.destroy()
    print 'Destruct requested for {0}, done here.'.format(STAGING_DO_NAME)

def _setup_ocean_get_token():
    try:
        import digitalocean
    except ImportError:
        print "You don't have the Digital Ocean API python bindings installed."
        print 'pip install python-digitalocean'
        print '.. in a virtualenv or in your root python setup (it only requires requests at time of writing)'
        print 'Then try again.'
        sys.exit(1)

    token = os.getenv('DOTOKEN')
    if not token:
        print 'Put your DO token in your shell env.'
        print 'E.g. keep "export DOTOKEN=thetoken" in <reporoot>/.dotoken.sh and do ". .dotoken.sh" before running Fabric'
        sys.exit(1)
    return token, digitalocean

def _find_staging_server(do_name):
    token, digitalocean = _setup_ocean_get_token()
    manager = digitalocean.Manager(token=token)
    droplets = manager.get_all_droplets()
    for d in droplets:
        if d.name == do_name:
            return d
    return None

def _setup_gate_for_staging(staging_private_ip):
    run('echo {0} > ~/repl/ips/staging'.format(staging_private_ip))

def _create_staging_server(do_name):
    token, digitalocean = _setup_ocean_get_token()
    manager = digitalocean.Manager(token=token)
    droplet = digitalocean.Droplet(
       token=token,
       name=do_name,
       region='lon1',
       image='11434212',  # basic-server-15-Apr-2015-2GB
       size_slug='2gb',  # 2GB ram, 2 VCPUs, 40GB SSD
       backups=False,
       private_networking=True,
       ssh_keys=['0a:f2:67:2c:d3:65:00:3d:70:7f:8e:e1:9a:8a:2c:7b', '81:54:2b:e7:dd:99:d5:89:c6:a6:cb:25:10:df:df:0b', '53:28:cc:e8:6c:28:aa:a4:67:5e:06:b2:64:b3:7e:ae', '20:91:2f:e3:a0:ba:9f:e3:30:ac:39:f3:8c:fb:11:a3', '33:b8:aa:6c:90:c3:f4:0f:4f:d0:2b:37:05:0e:8f:b4', 'bf:e2:5d:18:f5:ed:38:a1:59:b1:81:1e:0c:46:7c:54']
    )
    droplet.create()
    
    while droplet.status != 'active':
        print 'Waiting for staging droplet to become active. Current status {0}, droplet id {1}'.format(droplet.status, droplet.id)
        time.sleep(5)
        droplet = manager.get_droplet(droplet.id)

    print 'Staging droplet created, public IP {0}'.format(droplet.ip_address)
    return droplet.ip_address, droplet.private_ip_address

def _setup_staging_server():
    run('cd /opt/sysadmin')
    run('git config user.email "us@cottagelabs.com"')
    run('git config user.name "Cottage Labs LLP"')
    run('git pull')
    run('cp /opt/sysadmin/doaj/staging/authorized_keys ~/.ssh/authorized_keys')

    run('wget https://download.elastic.co/elasticsearch/elasticsearch/elasticsearch-1.4.4.deb')
    sudo('dpkg -i elasticsearch-1.4.4.deb')
    sudo('update-rc.d elasticsearch defaults 95 10')

    # make sure nginx (incl doaj-gate!) is correct, and supervisor (run through request to staging in head)
    # finish going through doajtest's command history
    # push hotfix with all new files

def __load(filename):
    with open(filename, 'rb') as f:
        content = f.read()
    return content