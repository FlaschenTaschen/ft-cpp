# Flaschen Taschen C++ - Top-level Makefile
# Routes build commands to subdirectories

.PHONY: all api client server examples hardware demos tools clean help

# Default target
all: api client server examples hardware demos tools

# Build individual components
api:
	$(MAKE) -C api/lib

client: api
	$(MAKE) -C client

server: api
	$(MAKE) -C server $(if $(FT_BACKEND),FT_BACKEND=$(FT_BACKEND))

examples: api
	$(MAKE) -C examples-api-use

hardware:
	$(MAKE) -C hardware

demos: api
	$(MAKE) -C demos

tools:
	$(MAKE) -C tools

# Clean all artifacts
clean:
	$(MAKE) -C api/lib clean
	$(MAKE) -C client clean
	$(MAKE) -C server clean
	$(MAKE) -C examples-api-use clean
	$(MAKE) -C hardware clean
	$(MAKE) -C demos clean
	$(MAKE) -C tools clean

# Help
help:
	@echo "Flaschen Taschen C++ Build System"
	@echo ""
	@echo "Targets:"
	@echo "  make              Build everything (api, client, server, examples, hardware, demos, tools)"
	@echo "  make api          Build libftclient library only"
	@echo "  make client       Build client tools (send-text, send-image, send-video)"
	@echo "  make server       Build server (FT_BACKEND=terminal by default)"
	@echo "  make examples     Build example programs"
	@echo "  make hardware     Build hardware utilities"
	@echo "  make demos        Build all demo implementations (examples, server, visual effects)"
	@echo "  make tools        Build utility tools (pixelate, etc.)"
	@echo "  make clean        Clean all build artifacts"
	@echo "  make help         Show this help message"
	@echo ""
	@echo "Optional targets:"
	@echo "  make -C client/game       Build game implementations (pong-game, etc.)"
	@echo "  make -C demos/src         Build visual effect demos (hack, black, plasma, etc.)"
	@echo ""
	@echo "FT_BACKEND options:"
	@echo "  terminal          Terminal output (default)"
	@echo "  ft                Real FlaschenTaschen hardware"
	@echo "  rgb-matrix        RGB LED matrix backend"
	@echo "  spixels           Spixels backend"
	@echo ""
	@echo "Example: make FT_BACKEND=ft server"

.PHONY: help
