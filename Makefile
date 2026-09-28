# Install the HID++ broker's system files (the Python package itself is
# installed by pip or the distribution package).
#
#   sudo make install-broker            into /usr/lib/...
#   make install-broker DESTDIR=pkgroot for packaging

PREFIX ?= /usr
UNITDIR ?= $(PREFIX)/lib/systemd/system
SYSUSERSDIR ?= $(PREFIX)/lib/sysusers.d
UDEVDIR ?= $(PREFIX)/lib/udev/rules.d

.PHONY: install-broker uninstall-broker

install-broker:
	install -Dm644 packaging/borochid-hidpp-broker.socket $(DESTDIR)$(UNITDIR)/borochid-hidpp-broker.socket
	install -Dm644 packaging/borochid-hidpp-broker.service $(DESTDIR)$(UNITDIR)/borochid-hidpp-broker.service
	install -Dm644 packaging/borochid-hidpp.sysusers.conf $(DESTDIR)$(SYSUSERSDIR)/borochid-hidpp.conf
	install -Dm644 packaging/70-borochid-logitech-keyboard.rules $(DESTDIR)$(UDEVDIR)/70-borochid-logitech-keyboard.rules

uninstall-broker:
	rm -f $(DESTDIR)$(UNITDIR)/borochid-hidpp-broker.socket $(DESTDIR)$(UNITDIR)/borochid-hidpp-broker.service
	rm -f $(DESTDIR)$(SYSUSERSDIR)/borochid-hidpp.conf $(DESTDIR)$(UDEVDIR)/70-borochid-logitech-keyboard.rules
