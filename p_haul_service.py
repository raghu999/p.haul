#
# P.HAUL code, that helps on the target node (rpyc service)
#

import rpyc
import os
import rpc_pb2 as cr_rpc
import p_haul_img as ph_img
import p_haul_criu as cr_api
import p_haul_socket as ph_sk
import p_haul_type

def init():
	ph_sk.start_listener()

class phaul_service(rpyc.Service):
	def on_connect(self):
		print "Connected"
		self.dump_iter = 0
		self.page_server_pid = 0
		self.restored = False
		self.img = ph_img.phaul_images() # FIXME -- get images driver from client
		self.criu = None

	def on_disconnect(self):
		print "Disconnected"
		if self.criu:
			self.criu.close()

		if self.page_server_pid:
			print "Sopping page server %d" % self.page_server_pid
			os.kill(self.page_server_pid, 9)

		if self.htype and not self.restored:
			self.htype.umount()

		print "Closing images"
		self.img.close(self.keep_images or not self.restored)

	def exposed_accept_mem_sk(self, name):
		# FIXME -- this may have not yet appeared, listener
		# is wating for the final SYN,ACK to arrive :(
		self.mem_sk = ph_sk.get_by_name(name)
		print "Mem sk accepted, name", self.mem_sk.name()
		self.criu = cr_api.criu_conn(self.mem_sk)

	def exposed_verbose(self, level):
		self.criu.verbose(level)

	def exposed_keep_images(self, v):
		self.keep_images = v

	def exposed_htype(self, id):
		print "Selecting htype to", id
		self.htype = p_haul_type.get_dst(id)

	def start_page_server(self):
		print "Starting page server for iter %d" % self.dump_iter

		req = cr_rpc.criu_req()
		req.type = cr_rpc.PAGE_SERVER
		req.keep_open = True
		req.opts.ps.fd = self.criu.mem_sk_fileno()

		req.opts.images_dir_fd = self.img.image_dir_fd()
		req.opts.work_dir_fd = self.img.work_dir_fd()
		p_img = self.img.prev_image_dir()
		if p_img:
			req.opts.parent_img = p_img

		print "\tSending criu rpc req"
		resp = self.criu.send_req(req)
		if (resp.type != cr_rpc.PAGE_SERVER) or (not resp.success):
			raise Exception("Failed to start page server")

		self.page_server_pid = resp.ps.pid
		print "\tPage server started at %d" % resp.ps.pid

	def exposed_start_iter(self):
		self.dump_iter += 1
		self.img.new_image_dir()
		self.start_page_server()

	def exposed_end_iter(self):
		self.page_server_pid = 0

	def exposed_start_accept_images(self):
		self.img_tar = ph_img.untar_thread(self.mem_sk, self.img.image_dir())
		self.img_tar.start()
		print "Started images server"

	def exposed_stop_accept_images(self):
		print "Waiting for images to unpack"
		self.img_tar.join()

	def exposed_restore_from_images(self):
		print "Restoring from images"
		self.htype.put_meta_images(self.img.image_dir())

		req = cr_rpc.criu_req()
		req.type = cr_rpc.RESTORE
		req.opts.images_dir_fd = self.img.image_dir_fd()
		req.opts.work_dir_fd = self.img.work_dir_fd()
		req.opts.notify_scripts = True

		if self.htype.can_migrate_tcp():
			req.opts.tcp_established = True

		for veth in self.htype.veths():
			v = req.opts.veths.add()
			v.if_in = veth.name
			v.if_out = veth.pair

		nroot = self.htype.mount()
		if nroot:
			req.opts.root = nroot
			print "Restore root set to %s" % req.opts.root

		cc = self.criu
		cc.send_req(req, False)

		while True:
			resp = cc.recv_resp()
			if resp.type == cr_rpc.NOTIFY:
				print "\t\tNotify (%s.%d)" % (resp.notify.script, resp.notify.pid)
				if resp.notify.script == "setup-namespaces":
					#
					# At that point we have only one task
					# living in namespaces and waiting for
					# us to ACK the notify. Htype might want
					# to configure namespace (external net
					# devices) and cgroups
					#
					self.htype.prepare_ct(resp.notify.pid)
				elif resp.notify.script == "network-unlock":
					self.htype.net_unlock()
				elif resp.notify.script == "network-lock":
					raise Exception("Locking network on restore?")

				cc.ack_notify()
				continue

			if resp.type != cr_rpc.RESTORE:
				raise Exception("Unexpected responce from service (%d)" % resp.type)

			if not resp.success:
				raise Exception("Restore failed")

			print "Restore succeeded"
			break

		self.htype.restored(resp.restore.pid)
		self.restored = True

	def exposed_restore_time(self):
		stats = cr_api.criu_get_rstats(self.img)
		return stats.restore_time
