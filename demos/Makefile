# Demos Makefile - Build all demo implementations
# Routes build commands to demo subdirectories

.PHONY: all examples src clean help

all: examples src

examples:
	$(MAKE) -C examples-api-use

src:
	$(MAKE) -C src

clean:
	$(MAKE) -C examples-api-use clean
	$(MAKE) -C src clean

help:
	@echo "Demos Build System"
	@echo ""
	@echo "Targets:"
	@echo "  make              Build all demos (examples and src visual effects)"
	@echo "  make examples     Build example demo programs"
	@echo "  make src          Build visual effect demos (hack, black, plasma, etc.)"
	@echo "  make clean        Clean all demo artifacts"
	@echo "  make help         Show this help message"
	@echo ""
	@echo "Visual effect demos in src/:"
	@echo "  make -C src hack              # Build specific demo"
	@echo "  make -C src                   # Build all visual effect demos"
	@echo ""

.PHONY: help
