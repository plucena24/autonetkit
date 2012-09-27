import autonetkit
import autonetkit.anm
import autonetkit.ank as ank
import math
import itertools
import autonetkit.ank_pika
import autonetkit.config
settings = autonetkit.config.settings
import autonetkit.log as log
import autonetkit.load.graphml as graphml
import autonetkit.exception


__all__ = ['build']

rabbitmq_server = settings['Rabbitmq']['server']
pika_channel = autonetkit.ank_pika.AnkPika(rabbitmq_server)

def build(input_filename):
    #TODO: move this out of main console wrapper
    anm = autonetkit.anm.AbstractNetworkModel()
    try:
        input_graph = graphml.load_graphml(input_filename)
    except autonetkit.exception.AnkIncorrectFileFormat:
# try a different reader
        try:
            import autonetkit.load.bug as bug
        except ImportError:
            return # module not present (development module)
        input_graph = bug.load(input_filename)
# add local deployment host
        settings['General']['deploy'] = True
        settings['Deploy Hosts']['internal'] = {
                'cisco': {
                    'deploy': True,
                    },
                }

    G_in = anm.add_overlay("input", input_graph)

    import autonetkit.plugins.graph_product as graph_product
    graph_product.expand(G_in) # apply graph products if relevant
    
    if len(ank.unique_attr(G_in, "asn")) > 1:
        # Multiple ASNs set, use label format device.asn 
        anm.set_node_label(".",  ['label', 'pop', 'asn'])

# set syntax for routers according to platform
#TODO: make these defaults
    G_in.update(G_in.nodes("is_router", platform = "junosphere"), syntax="junos")
    G_in.update(G_in.nodes("is_router", platform = "dynagen"), syntax="ios")
    G_in.update(G_in.nodes("is_router", platform = "netkit"), syntax="quagga")
    #G_in.update(G_in.nodes("is_router", platform = "cisco"), syntax="ios")

    G_graphics = anm.add_overlay("graphics") # plotting data
    G_graphics.add_nodes_from(G_in, retain=['x', 'y', 'device_type', 'device_subtype', 'pop', 'asn'])

    build_phy(anm)
    build_ip(anm)
    build_ospf(anm)
    build_isis(anm)
    build_bgp(anm)
    return anm


def build_bgp(anm):
    # eBGP
    G_phy = anm['phy']
    G_in = anm['input']
    G_bgp = anm.add_overlay("bgp", directed = True)
    G_bgp.add_nodes_from(G_in.nodes("is_router"))
    ebgp_edges = [edge for edge in G_in.edges() if not edge.attr_equal("asn")]
    G_bgp.add_edges_from(ebgp_edges, bidirectional = True, type = 'ebgp')

# now iBGP
    if len(G_phy) < 5:
# full mesh
        for asn, devices in G_phy.groupby("asn").items():
            routers = [d for d in devices if d.is_router]
            ibgp_edges = [ (s, t) for s in routers for t in routers if s!=t]
            G_bgp.add_edges_from(ibgp_edges, type = 'ibgp')
    else:
        import autonetkit.plugins.route_reflectors as route_reflectors
        route_reflectors.allocate(G_phy, G_bgp)

#TODO: probably want to use l3 connectivity graph for allocating route reflectors

    ebgp_nodes = [d for d in G_bgp if any(edge.type == 'ebgp' for edge in d.edges())]
    G_bgp.update(ebgp_nodes, ebgp=True)

def build_ip(anm):
    import autonetkit.plugins.ip as ip
    G_ip = anm.add_overlay("ip")
    G_in = anm['input']
    G_graphics = anm['graphics']
    G_phy = anm['phy']

    G_ip.add_nodes_from(G_in)
    G_ip.add_edges_from(G_in.edges(type="physical"))

    ank.aggregate_nodes(G_ip, G_ip.nodes("is_switch"), retain = "edge_id")
#TODO: add function to update edge properties: can overload node update?

    edges_to_split = [edge for edge in G_ip.edges() if edge.attr_both("is_l3device")]
    split_created_nodes = list(ank.split(G_ip, edges_to_split, retain='edge_id'))
    for node in split_created_nodes:
        node.overlay.graphics.x = ank.neigh_average(G_ip, node, "x", G_graphics)
        node.overlay.graphics.y = ank.neigh_average(G_ip, node, "y", G_graphics)
        node.overlay.graphics.asn = ank.neigh_most_frequent(G_ip, node, "asn", G_phy) # arbitrary choice
#TODO: could choose largest ASN if tie break

    switch_nodes = G_ip.nodes("is_switch")# regenerate due to aggregated
    G_ip.update(switch_nodes, collision_domain=True) # switches are part of collision domain
    G_ip.update(split_created_nodes, collision_domain=True)
# Assign collision domain to a host if all neighbours from same host
    for node in split_created_nodes:
        if ank.neigh_equal(G_ip, node, "host", G_phy):
            node.host = ank.neigh_attr(G_ip, node, "host", G_phy).next() # first attribute

# set collision domain IPs
#TODO: trim next line
    collision_domain_id = itertools.count(0)
    for node in G_ip.nodes("collision_domain"):
        graphics_node = G_graphics.node(node)
        graphics_node.device_type = "collision_domain"
        cd_id = collision_domain_id.next()
        node.cd_id = cd_id
#TODO: Use this label
        if not node.is_switch:
            label = "_".join(sorted(ank.neigh_attr(G_ip, node, "label", G_phy)))
            cd_label = "cd_%s" % label # switches keep their names
            node.label = cd_label 
            node.cd_id = cd_label
            graphics_node.label = cd_label


    ip.allocate_ips(G_ip)
    ank.save(G_ip)


def build_phy(anm):
    G_in = anm['input']
    G_phy = anm['phy']
    G_phy.add_nodes_from(G_in, retain=['label', 'update', 'device_type', 'device_subtype', 'asn', 'platform', 'host', 'syntax'])
    if G_in.data.Creator == "Topology Zoo Toolset":
        ank.copy_attr_from(G_in, G_phy, "Network") # Copy Network from Zoo
    G_phy.add_edges_from(G_in.edges(type="physical"))


def build_ospf(anm):
    G_in = anm['input']
    G_ospf = anm.add_overlay("ospf")
    G_ospf.add_nodes_from(G_in.nodes("is_router"), retain=['asn'])
    #update_pika(anm)
    G_ospf.add_nodes_from(G_in.nodes("is_switch"), retain=['asn'])
    #update_pika(anm)
    G_ospf.add_edges_from(G_in.edges(), retain = ['edge_id'])

    #update_pika(anm)
    ank.aggregate_nodes(G_ospf, G_ospf.nodes("is_switch"), retain = "edge_id")
    #update_pika(anm)
    ank.explode_nodes(G_ospf, G_ospf.nodes("is_switch"), retain= "edge_id")
    #update_pika(anm)

    #update_pika(anm)
    G_ospf.remove_edges_from([link for link in G_ospf.edges() if link.src.asn != link.dst.asn])
    for link in G_ospf.edges():
        link.area = 0
        link.cost = 1

    #update_pika(anm)

def ip_to_net_ent_title_ios(ip):
    """ Converts an IP address into an OSI Network Entity Title
    suitable for use in IS-IS on IOS.

    >>> ip_to_net_ent_title_ios(IPAddress("192.168.19.1"))
    '49.1921.6801.9001.00'
    """
    try:
        ip_words = ip.words
    except AttributeError:
        import netaddr # try to cast to IP Address
        ip = netaddr.IPAddress(ip)
        ip_words = ip.words

    log.debug("Converting IP to OSI ENT format")
    area_id = "49"
    ip_octets = "".join("%03d" % int(octet) for octet in ip_words) # single string, padded if needed
    return ".".join([area_id, ip_octets[0:4], ip_octets[4:8], ip_octets[8:12], "00"])

def build_isis(anm):
    G_in = anm['input']
    G_ip = anm['ip']
    G_isis = anm.add_overlay("isis")
    #G_isis.add_nodes_from(G_in.nodes("is_router", igp = "isis"), retain=['asn'])
#TODO: filter only igp=isis nodes, set the igp as a default in build_network
    G_isis.add_nodes_from(G_in.nodes("is_router"), retain=['asn'])
    G_isis.add_nodes_from(G_in.nodes("is_switch"), retain=['asn'])
    G_isis.add_edges_from(G_in.edges(), retain = ['edge_id'])
# Merge and explode switches
    ank.aggregate_nodes(G_isis, G_isis.nodes("is_switch"), retain = "edge_id")
    ank.explode_nodes(G_isis, G_isis.nodes("is_switch"), retain = "edge_id")

    G_isis.remove_edges_from([link for link in G_isis.edges() if link.src.asn != link.dst.asn])

    for node in G_isis:
        ip_node = G_ip.node(node)
        node.net = ip_to_net_ent_title_ios(ip_node.loopback)

def update_pika(anm):
    log.debug("Sending anm to pika")
    body = autonetkit.ank_json.dumps(anm, None)
    pika_channel.publish_compressed("www", "client", body)
