#! /usr/bin/python

from Tardis import Defaults, Util, TardisDB, TardisCrypto
import os.path
import logging
import argparse
import base64

from Cryptodome.Cipher import AES

def processArgs():
    parser = argparse.ArgumentParser(description='Set a token/password')
    parser.add_argument('--database', '-D', dest='database', default=Defaults.getDefault('TARDIS_DB'),      help="Database to use.  Default: %(default)s")
    parser.add_argument('--client', '-C',   dest='client',   default=Defaults.getDefault('TARDIS_CLIENT'),  help="Client to list on.  Default: %(default)s")
    parser.add_argument('--dbname',         dest='dbname',   default=Defaults.getDefault('TARDIS_DBNAME'),  help="Name of the database file. Default: %(default)s")

    passgroup= parser.add_argument_group("Password/Encryption specification options")
    pwgroup = passgroup.add_mutually_exclusive_group(required=True)
    pwgroup.add_argument('--password', '-p',dest='password', default=None, nargs='?', const=True,       help='Encrypt files with this password')
    pwgroup.add_argument('--password-file', dest='passwordfile', default=None,                          help='Read password from file')
    pwgroup.add_argument('--password-url',  dest='passwordurl', default=None,                           help='Retrieve password from the specified URL')
    pwgroup.add_argument('--password-prog', dest='passwordprog', default=None,                          help='Use the specified command to generate the password on stdout')

    return parser.parse_args()

def createToken(crypto, client):
    cipher = AES.new(crypto._tokenKey, AES.MODE_ECB)
    token = base64.b64encode(cipher.encrypt(crypto.padzero(client)), crypto._altchars)
    return token

def main():
    logging.basicConfig(level=logging.DEBUG)
    args = processArgs()
    password = Util.getPassword(args.password, args.passwordfile, args.passwordurl, args.passwordprog)

    crypto = TardisCrypto.TardisCrypto(password, args.client)
    token = createToken(crypto, args.client)

    path = os.path.join(args.database, args.client, args.dbname)
    db = TardisDB.TardisDB(path, backup=False)
    db.setToken(token)

if __name__ == "__main__":
    main()
